"""Trading report page: paper trades (the bot's own trade log) and LIVE trades (the account's real fills, fetched read-only
from Kite after the close), one static self-contained HTML page served by Caddy at a fixed secret link.

    data/live/YYYY-MM-DD.json   one file per trading day: the account's option fills and order tags (Kite keeps only
                                the current day, so each day is saved at the close; a day the VM was off is missing)
    data/site/index.html        the page (rebuilt at start-up and after each close; atomic replace)
    data/site/.hash             fingerprint of the page's data: the Telegram notice is sent only when it changes

Nothing here places or changes orders: Kite is only read (orders(), trades()).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
from collections import defaultdict
from typing import Any, Callable, Iterable, Optional

from costs import CostParams, trade_cost

log = logging.getLogger(__name__)

OPTION_EXCHANGES = {"BFO", "NFO"}
BOT_TAG_PREFIX = "SXALGO"
_FILL_FIELDS = ("trade_id", "order_id", "exchange", "tradingsymbol", "product", "average_price", "quantity",
                "transaction_type", "fill_timestamp")
_ORDER_FIELDS = ("order_id", "tag", "tradingsymbol", "status", "transaction_type", "product", "exchange")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)                      # Caddy (another container) must be able to read the page
    os.replace(tmp, path)


# ------------------------------------------------------------------ live: save the day's real fills
def save_live_day(day_orders: list[dict], day_trades: list[dict], day: dt.date, live_dir: str) -> dict:
    """Keep only option fills/orders and only the fields the report needs (no account ids). Returns the record."""
    fills = [{k: _iso(t.get(k)) for k in _FILL_FIELDS} for t in day_trades
             if t.get("exchange") in OPTION_EXCHANGES]
    orders = [{k: _iso(o.get(k)) for k in _ORDER_FIELDS} for o in day_orders
              if o.get("exchange") in OPTION_EXCHANGES]
    rec = {"date": day.isoformat(), "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "fills": fills, "orders": orders}
    _atomic_write(os.path.join(live_dir, f"{day.isoformat()}.json"), json.dumps(rec))
    return rec


def load_live(live_dir: str, since: str) -> list[dict]:
    out = []
    if not os.path.isdir(live_dir):
        return out
    for name in sorted(os.listdir(live_dir)):
        if not name.endswith(".json") or name[:10] < since:
            continue
        try:
            with open(os.path.join(live_dir, name), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            log.warning("Unreadable live record %s", name)
    return out


def live_trades(records: Iterable[dict], costs: CostParams) -> tuple[list[dict], list[str]]:
    """One row per (day, contract): bought / sold quantity and average, realised P&L of the closed quantity, estimated
    charges (Zerodha schedule, per order), open quantity left. Returns (rows, days fetched)."""
    rows, days = [], []
    for rec in records:
        day = rec["date"]
        days.append(day)
        tags = {o["order_id"]: (o.get("tag") or "") for o in rec.get("orders", [])}
        by_sym: dict[str, list[dict]] = defaultdict(list)
        for f in rec.get("fills", []):
            by_sym[f["tradingsymbol"]].append(f)
        for sym, fills in sorted(by_sym.items()):
            fills.sort(key=lambda f: str(f.get("fill_timestamp") or ""))
            bq = sum(int(f["quantity"]) for f in fills if f["transaction_type"] == "BUY")
            sq = sum(int(f["quantity"]) for f in fills if f["transaction_type"] == "SELL")
            bv = sum(int(f["quantity"]) * float(f["average_price"]) for f in fills if f["transaction_type"] == "BUY")
            sv = sum(int(f["quantity"]) * float(f["average_price"]) for f in fills if f["transaction_type"] == "SELL")
            closed = min(bq, sq)
            gross = (sv / sq - bv / bq) * closed if closed else 0.0
            per_order: dict[str, list] = {}
            for f in fills:                     # brokerage is per order: cost each order once, at its average fill
                o = per_order.setdefault(f["order_id"], [f["transaction_type"], 0.0, 0])
                o[1] += float(f["average_price"]) * int(f["quantity"]); o[2] += int(f["quantity"])
            cost = trade_cost([(s, v / q, q) for s, v, q in per_order.values() if q], costs)
            bot = any(str(tags.get(f["order_id"], "")).startswith(BOT_TAG_PREFIX) for f in fills)
            t0 = str(fills[0].get("fill_timestamp") or "")[11:16]
            t1 = str(fills[-1].get("fill_timestamp") or "")[11:16]
            rows.append({"date": day, "symbol": sym, "who": "Bot" if bot else "Manual", "entry": t0, "exit": t1,
                         "bought": bq, "buy_avg": round(bv / bq, 2) if bq else None,
                         "sold": sq, "sell_avg": round(sv / sq, 2) if sq else None,
                         "open_qty": bq - sq, "gross": round(gross, 2), "cost": round(cost, 2),
                         "net": round(gross - cost, 2)})
    return rows, days


# ------------------------------------------------------------------ paper: the bot's own trade log
def _setup_name(variant: str) -> str:
    if variant.startswith("pb_single_"):
        return f"Single leg {variant.rsplit('_', 1)[1].title()}"
    if variant == "pb_condor":
        return "Iron condor"
    if variant.startswith("pb_credit_"):
        return "Bull put spread" if variant.endswith("_UP") else "Bear call spread"
    return variant


def paper_trades(rows: Iterable[tuple]) -> list[dict]:
    """Pair each closed trade ('exit' row: whole-trade gross/cost/net) with its entry row."""
    open_entries: dict[str, list[tuple]] = defaultdict(list)
    out = []
    for ts, variant, event, reason, gross, cost, net, payload in rows:
        if event == "entry":
            open_entries[variant].append((ts, payload))
        elif event == "exit":
            ets, ep = open_entries[variant].pop(0) if open_entries[variant] else (ts, "{}")
            try:
                p = json.loads(ep or "{}")
            except ValueError:
                p = {}
            out.append({"date": ets[:10], "setup": _setup_name(variant), "variant": variant,
                        "entry": ets[11:16], "exit": ts[11:16], "lots": p.get("lots"),
                        "spot": p.get("spot"), "vix": p.get("vix"), "why": str(p.get("rationale", ""))[:220],
                        "reason": reason, "gross": round(gross or 0, 2), "cost": round(cost or 0, 2),
                        "net": round(net or 0, 2)})
    return out


# ------------------------------------------------------------------ page
def page_data(paper: list[dict], paper_days: list[str], live: list[dict], live_days: list[str], since: str,
              backtest_url: str) -> dict:
    return {"since": since, "paper": {"trades": paper, "days": sorted(set(paper_days))},
            "live": {"trades": live, "days": sorted(set(live_days))}, "backtest_url": backtest_url}


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def publish_gcs(html: str, bucket: str, obj: str, http=None) -> bool:
    """Overwrite ONE fixed object (gs://bucket/obj) with the page, so its public link never changes. Uses the VM's own
    service-account token from the metadata server (no key file, no extra package). cacheControl no-cache: readers see
    an update at once instead of a cached copy. Never raises; returns success."""
    if http is None:
        import requests as http
    try:
        tok = http.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
        tok.raise_for_status()
        access = tok.json()["access_token"]
        boundary = "sxbot-report-" + hashlib.sha256(html.encode()).hexdigest()[:24]
        meta = json.dumps({"name": obj, "contentType": "text/html; charset=utf-8",
                           "cacheControl": "no-cache, max-age=0"})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: text/html; charset=utf-8\r\n\r\n").encode() + html.encode() + \
            f"\r\n--{boundary}--\r\n".encode()
        r = http.post(f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o",
                      params={"uploadType": "multipart"}, data=body, timeout=20,
                      headers={"Authorization": f"Bearer {access}",
                               "Content-Type": f"multipart/related; boundary={boundary}"})
        if not r.ok:
            log.warning("Report upload to Cloud Storage refused (HTTP %s)", r.status_code)   # never log the token
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - the mirror must never disturb trading
        log.warning("Report upload to Cloud Storage failed: %s", exc.__class__.__name__)
        return False


def render(data: dict, generated_at: str) -> str:
    blob = json.dumps({**data, "generated_at": generated_at}).replace("<", "\\u003c")   # no </script> break-out
    return PAGE.replace("/*__DATA__*/null", blob)


class ReportSite:
    def __init__(self, data_dir: str, url: Optional[str], since: str, costs: CostParams,
                 backtest_url: str = "", gcs_bucket: str = "", gcs_object: str = "") -> None:
        self.live_dir = os.path.join(data_dir, "live")
        self.site_dir = os.path.join(data_dir, "site")
        self.since, self.costs, self.backtest_url = since, costs, backtest_url
        self.vm_url = url
        self.gcs_bucket, self.gcs_object = gcs_bucket, gcs_object
        self.gcs_url = (f"https://storage.googleapis.com/{gcs_bucket}/{gcs_object}"
                        if gcs_bucket and gcs_object else None)
        self.url = self.gcs_url or url              # the always-on mirror when configured, else the VM link
        self.publish = publish_gcs

    def save_live(self, client, day: dt.date) -> Optional[dict]:
        """Fetch and store today's real fills. Never raises (the report must not disturb trading)."""
        try:
            return save_live_day(client.day_orders(), client.day_trades(), day, self.live_dir)
        except Exception:  # noqa: BLE001
            log.exception("Live fills fetch failed")
            return None

    def build(self, rec, paper_days: list[str]) -> tuple[bool, dict]:
        """Rebuild the page. Returns (data changed since the last build, today's summary). Never raises."""
        try:
            paper = paper_trades(rec.trades_since(self.since, "PAPER"))
            live, live_days = live_trades(load_live(self.live_dir, self.since), self.costs)
            data = page_data(paper, paper_days, live, live_days, self.since, self.backtest_url)
            fp = fingerprint(data)
            hpath = os.path.join(self.site_dir, ".hash")
            try:
                with open(hpath, encoding="utf-8") as f:
                    old = f.read().strip()
            except OSError:
                old = ""
            now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            html = render(data, now)
            _atomic_write(os.path.join(self.site_dir, "index.html"), html)
            if self.gcs_url and not self.publish(html, self.gcs_bucket, self.gcs_object):
                return False, {"paper": paper, "live": live}   # keep the old hash: retried (and notified) next build
            _atomic_write(hpath, fp)
            return fp != old, {"paper": paper, "live": live}
        except Exception:  # noqa: BLE001
            log.exception("Report page build failed")
            return False, {}

    def summary_text(self, day: str, summary: dict) -> str:
        def part(name: str, rows: list[dict]) -> str:
            r = [x for x in rows if x["date"] == day]
            if not r:
                return f"{name}: no trades"
            return f"{name}: net {sum(x['net'] for x in r):+,.0f} over {len(r)} trade{'s' if len(r) != 1 else ''}"
        return (f"Trading report updated for {day}. {part('Paper', summary.get('paper', []))}; "
                f"{part('Live', summary.get('live', []))}.\n{self.url or '(report link not configured)'}")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>SENSEX Bot Trading Report</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#17202b;--muted:#5d6b7a;--line:#dfe4ea;--ok:#147a4b;--bad:#b42b2b;--accent:#2f5d8a;--zero:#9aa5b1;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f141a;--panel:#161d25;--fg:#e6ebf0;--muted:#97a3af;--line:#2a3440;--ok:#4cc38a;--bad:#f07171;--accent:#7fb0e0;--zero:#5d6b7a;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-right)) 48px max(16px,env(safe-area-inset-left));display:flex;flex-direction:column;gap:20px}
h1{font:400 28px/1.2 Georgia,"Times New Roman",serif;margin:0}h2{font-size:16px;font-weight:600;margin:0}
.muted{color:var(--muted)}.note{font-size:12px;color:var(--muted);margin:0}
.tabs{display:flex;gap:8px}.tabs button{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--fg);cursor:pointer}
.tabs button[aria-selected=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,11rem),1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0;display:flex;flex-direction:column;gap:8px}
.tile .v{font:400 26px/1.1 Georgia,serif;font-variant-numeric:tabular-nums}.tile .l{font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,26rem),1fr));gap:12px}
.scroll{overflow:auto;max-height:34rem}.wide{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{text-align:left;font-weight:600;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}.num{text-align:right;white-space:nowrap}
.pos{color:var(--ok)}.neg{color:var(--bad)}.zero{color:var(--muted)}.detail{color:var(--muted);min-width:16rem}
.chart{position:relative;min-height:220px}.tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.15);max-width:22rem}
.empty{padding:24px;text-align:center;color:var(--muted)}a{color:var(--accent)}
</style></head><body><div class="wrap">
<header style="display:flex;flex-direction:column;gap:6px">
<h1>SENSEX bot trading report</h1>
<p class="muted" id="lede" style="margin:0"></p>
</header>
<div class="tabs" role="tablist"><button role="tab" id="tab-paper" aria-selected="true">Paper trading</button><button role="tab" id="tab-live" aria-selected="false">Live trading</button></div>
<section id="view"></section>
<p class="note" id="foot"></p>
</div>
<script>
const D=/*__DATA__*/null;
const inr=new Intl.NumberFormat('en-IN',{maximumFractionDigits:0});
const sg=v=>(v>0?'+':'')+inr.format(v);const cls=v=>v>0?'pos':v<0?'neg':'zero';
const el=(t,a={},...k)=>{const e=document.createElement(t);for(const[x,y]of Object.entries(a)){if(x==='class')e.className=y;else e.setAttribute(x,y)}for(const c of k)e.append(c instanceof Node?c:document.createTextNode(c==null?'':String(c)));return e};
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function daysOf(kind){const rows=D[kind].trades;const set=new Set(D[kind].days);rows.forEach(r=>set.add(r.date));return[...set].sort()}
function daily(kind){const m=new Map(daysOf(kind).map(d=>[d,{date:d,net:0,n:0,items:[]}]));for(const r of D[kind].trades){const o=m.get(r.date);o.net+=r.net;o.n+=1;o.items.push(r)}return[...m.values()]}
function stats(kind){const t=D[kind].trades.map(r=>r.net),w=t.filter(x=>x>0),l=t.filter(x=>x<0);const dd=daily(kind);let eq=0,pk=0,mdd=0;for(const d of dd){eq+=d.net;pk=Math.max(pk,eq);mdd=Math.max(mdd,pk-eq)}
return{net:t.reduce((a,b)=>a+b,0),n:t.length,win:t.length?w.length/t.length:0,pf:l.length?w.reduce((a,b)=>a+b,0)/-l.reduce((a,b)=>a+b,0):null,days:dd.length,wd:dd.filter(d=>d.net>0).length,ld:dd.filter(d=>d.net<0).length,idle:dd.filter(d=>d.n===0).length,best:dd.length?Math.max(...dd.map(d=>d.net)):0,worst:dd.length?Math.min(...dd.map(d=>d.net)):0,mdd}}
function tile(label,value,c,sub){return el('div',{class:'card tile'},el('span',{class:'l'},label),el('span',{class:'v '+(c||'')},value),sub?el('span',{class:'l'},sub):'')}
function table(head,rows,numCols){const t=el('table');const tr=el('tr');head.forEach((h,i)=>tr.append(el('th',{class:numCols.includes(i)?'num':''},h)));t.append(el('thead',{},tr));const tb=el('tbody');rows.forEach(r=>{const x=el('tr');r.forEach((c,i)=>{const td=el('td',{class:(numCols.includes(i)?'num ':'')+(c&&c.c||'')},c&&c.t!==undefined?c.t:c);x.append(td)});tb.append(x)});t.append(tb);return t}
function chart(dd){const box=el('div',{class:'chart'});requestAnimationFrame(()=>{const W=box.clientWidth||600,H=220,L=56,B=22;if(!dd.length){box.append(el('div',{class:'empty'},'No trading days yet.'));return}
const vals=dd.map(d=>d.net);let lo=Math.min(0,...vals),hi=Math.max(0,...vals);if(lo===hi)hi=lo+1;const y=v=>8+(H-B-8)*(hi-v)/(hi-lo);const bw=(W-L-4)/dd.length;
const ns='http://www.w3.org/2000/svg';const s=document.createElementNS(ns,'svg');s.setAttribute('viewBox',`0 0 ${W} ${H}`);s.setAttribute('width','100%');s.setAttribute('role','img');s.setAttribute('aria-label','Daily P&L');
const mk=(t,a)=>{const e=document.createElementNS(ns,t);for(const[k,v]of Object.entries(a))e.setAttribute(k,v);s.append(e);return e};
for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;const yy=y(v);mk('line',{x1:L,x2:W-4,y1:yy,y2:yy,stroke:'var(--line)'});const tx=mk('text',{x:L-6,y:yy+4,'text-anchor':'end','font-size':11,fill:'var(--muted)'});tx.textContent=Math.abs(v)>=1000?(v/1000).toFixed(1)+'k':Math.round(v)}
mk('line',{x1:L,x2:W-4,y1:y(0),y2:y(0),stroke:'var(--muted)'});
dd.forEach((d,i)=>{if(d.net!==0){mk('rect',{x:L+i*bw+bw*.1,width:Math.max(1,bw*.8),y:Math.min(y(0),y(d.net)),height:Math.max(1,Math.abs(y(d.net)-y(0))),rx:Math.min(2,bw/3),fill:d.net>0?'var(--ok)':'var(--bad)'})}});
let prev='';dd.forEach((d,i)=>{const m=d.date.slice(0,7);if(m!==prev&&(dd.length<40||i===0||d.date.slice(8)<='07')){prev=m;const tx=mk('text',{x:L+i*bw,y:H-6,'font-size':11,fill:'var(--muted)'});tx.textContent=MON[+d.date.slice(5,7)-1]+' '+d.date.slice(2,4)}});
box.append(s);const tip=el('div',{class:'tip',hidden:''});box.append(tip);
s.addEventListener('pointermove',ev=>{const r=s.getBoundingClientRect();const sx=(ev.clientX-r.left)*W/r.width;const i=Math.floor((sx-L)/bw);const d=dd[i];if(!d){tip.hidden=true;return}tip.replaceChildren(el('div',{class:'muted'},d.date),el('div',{},d.n?('Day '+sg(d.net)+' over '+d.n+' trade'+(d.n>1?'s':'')):'No trade'));tip.hidden=false;const bx=ev.clientX-box.getBoundingClientRect().left;tip.style.left=Math.max(0,Math.min(W-tip.offsetWidth,bx+12))+'px';tip.style.top='8px'});
s.addEventListener('pointerleave',()=>{tip.hidden=true})});return box}
function view(kind){const v=document.getElementById('view');v.replaceChildren();const st=stats(kind);const dd=daily(kind);const wrap=el('div',{style:'display:flex;flex-direction:column;gap:12px'});
const isLive=kind==='live';
if(!dd.length){wrap.append(el('div',{class:'card empty'},isLive?'No live trading days recorded yet. The account\'s real fills are fetched from Kite after each close.':'No paper trades yet. Paper trading with the playbook starts on the first trading day.'));v.append(wrap);return}
const tl=el('div',{class:'tiles'});tl.append(tile('Net P&L since '+D.since+' (Rs)',sg(st.net),cls(st.net),isLive?'Realised, after estimated charges':'After charges, paper fills at bid/ask'),tile('Trades',inr.format(st.n),'',st.pf==null?'':'Profit factor '+st.pf.toFixed(2)),tile('Win rate',Math.round(st.win*100)+'%','',''),tile('Days: won / lost / no trade',st.wd+' / '+st.ld+' / '+st.idle,'',''),tile('Best day / worst day (Rs)',sg(st.best)+' / '+sg(st.worst),'',''),tile('Largest drop from a peak (Rs)',sg(-st.mdd),st.mdd>0?'neg':'',''));wrap.append(tl);
const c1=el('div',{class:'card'},el('h2',{},'Daily P&L'),el('p',{class:'note'},'Every trading day recorded. Bars above the line made money.'),chart(dd));wrap.append(c1);
const g=el('div',{class:'grid2'});
const mon=new Map();dd.forEach(d=>{const k=d.date.slice(0,7);const o=mon.get(k)||{k,net:0,n:0,w:0,l:0};o.net+=d.net;o.n+=d.n;if(d.net>0)o.w++;else if(d.net<0)o.l++;mon.set(k,o)});
g.append(el('div',{class:'card'},el('h2',{},'By month'),el('div',{class:'wide'},table(['Month','Trades','Won days','Lost days','Net (Rs)'],[...mon.values()].reverse().map(o=>[MON[+o.k.slice(5)-1]+' '+o.k.slice(0,4),inr.format(o.n),o.w,o.l,{t:sg(o.net),c:cls(o.net)}]),[1,2,3,4]))));
const key=isLive?'who':'setup';const by=new Map();D[kind].trades.forEach(r=>{const o=by.get(r[key])||{k:r[key],n:0,net:0,w:0};o.n++;o.net+=r.net;if(r.net>0)o.w++;by.set(r[key],o)});
g.append(el('div',{class:'card'},el('h2',{},isLive?'Bot and manual orders':'By setup'),el('div',{class:'wide'},table([isLive?'Placed by':'Setup','Trades','Win rate','Net (Rs)'],[...by.values()].map(o=>[o.k,inr.format(o.n),Math.round(100*o.w/o.n)+'%',{t:sg(o.net),c:cls(o.net)}]),[1,2,3])),isLive?el('p',{class:'note'},'Bot = orders tagged by the bot; Manual = every other option order on the account.'):''));
wrap.append(g);
const rows=[...dd].reverse().map(d=>[d.date,inr.format(d.n),{t:d.n?sg(d.net):'0',c:cls(d.net)},d.n?d.items.map(r=>isLive?`${r.symbol} [${r.who}] bought ${r.bought}${r.buy_avg?' @ '+r.buy_avg:''}, sold ${r.sold}${r.sell_avg?' @ '+r.sell_avg:''} (${r.entry}-${r.exit})${r.open_qty?' OPEN '+r.open_qty:''}: ${sg(r.net)} (charges ${inr.format(r.cost)})`:`${r.setup} x${r.lots??'?'} ${r.entry}-${r.exit} ${String(r.reason).replace(/_/g,' ').toLowerCase()}: ${sg(r.net)}`).join('; '):'No trade'].map((c,i)=>i===3?{t:c,c:'detail'}:c));
wrap.append(el('div',{class:'card'},el('h2',{},'Every day'),el('div',{class:'scroll'},table(['Date','Trades','Day net (Rs)','Trades and exits'],rows,[1,2]))));
if(isLive)wrap.append(el('p',{class:'note'},'Live P&L is realised P&L per contract per day from the account\'s fills; charges are estimated from Zerodha\'s schedule, so the contract note can differ by a few rupees. A day the VM was off at the close is missing, because Kite only keeps the current day\'s orders.'));
v.append(wrap)}
function show(kind){document.getElementById('tab-paper').setAttribute('aria-selected',kind==='paper');document.getElementById('tab-live').setAttribute('aria-selected',kind==='live');view(kind);try{history.replaceState(null,'','#'+kind)}catch(e){}}
document.getElementById('tab-paper').onclick=()=>show('paper');document.getElementById('tab-live').onclick=()=>show('live');
const ps=stats('paper'),ls=stats('live');
const tn=n=>inr.format(n)+(n===1?' trade':' trades');
document.getElementById('lede').textContent=`Results since ${D.since}. Paper: ${sg(ps.net)} over ${tn(ps.n)}. Live: ${sg(ls.net)} over ${tn(ls.n)}.`;
const f=document.getElementById('foot');f.append('Updated '+D.generated_at.replace('T',' ')+'. ');if(D.backtest_url){f.append('Backtest for comparison: ');f.append(el('a',{href:D.backtest_url,target:'_blank',rel:'noopener noreferrer'},'SENSEX playbook backtest'))}
show(location.hash==='#live'?'live':'paper');
</script></body></html>"""
