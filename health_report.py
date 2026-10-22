"""Is the bot working? Read-only report from data/sxbot.db and the approval files.

Run on the VM:  docker compose exec bot python health_report.py [--date YYYY-MM-DD]
The bot also calls collect() every 15 minutes to append a row to the "Health" tab of your Google Sheet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from typing import Any, Optional

from config import IST, Config, load_config
from token_store import is_fresh, read_token


def _q(con, sql, *a):
    return con.execute(sql, a).fetchall()


def _approvals(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def collect(cfg: Config, now: dt.datetime, day: Optional[str] = None) -> dict[str, Any]:
    """Gather health facts (never raises on a missing DB). Returns {'info': {...}, 'flags': [...]}."""
    day = day or now.date().isoformat()
    lo, hi = day, day + "~"                    # range scan on the ts index ('~' sorts after any time suffix)
    flags: list[str] = []
    info: dict[str, Any] = {"day": day}
    token_ok = is_fresh(read_token(cfg.paths.token_file))
    info["token_fresh"] = token_ok
    if not token_ok:
        flags.append("Not logged in to Kite today: no session, no data.")
    for key, path in (("paper_approved", cfg.paths.approved_backtest), ("live_approved", cfg.paths.approved_live)):
        d = _approvals(path)
        info[key] = len(d.get("variants", [])) if d else 0
        info[key + "_until"] = d.get("valid_until") if d else None
        if d is None and key == "paper_approved":
            flags.append("No approved variants: data is recorded and shadow-traded but NO paper orders are placed. "
                         "Run: python backtest.py --fetch-kite 700 (then restart the bot).")
    info.update(market_rows=0, chain_rows=0, last_ts=None, age_s=None, spot=None, vix=None, regime=None,
                score=None, spike=None, shadow_closed=0, shadow_net=0.0, shadow_wins=0, trades=[],
                events={}, best=[], worst=[], days=0, shadow_total=0)
    if not os.path.exists(cfg.paths.db):
        flags.append("Database not created yet.")
        return {"info": info, "flags": flags}
    con = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True, timeout=5)
    try:
        info["market_rows"] = _q(con, "SELECT COUNT(*) FROM market WHERE ts>=? AND ts<?", lo, hi)[0][0]
        info["chain_rows"] = _q(con, "SELECT COUNT(*) FROM chain WHERE ts>=? AND ts<?", lo, hi)[0][0]
        if info["market_rows"]:
            r = _q(con, "SELECT ts, spot, vix, regime, score, spike FROM market WHERE ts>=? AND ts<? "
                        "ORDER BY ts DESC LIMIT 1", lo, hi)[0]
            info.update(last_ts=r[0], spot=r[1], vix=r[2], regime=r[3], score=r[4], spike=bool(r[5]))
            info["age_s"] = (now - dt.datetime.fromisoformat(r[0])).total_seconds()
        in_hours = day == now.date().isoformat() and now.weekday() < 5 and dt.time(9, 20) <= now.time() <= dt.time(15, 30)
        if in_hours and info["market_rows"] and info["age_s"] > 120:
            flags.append(f"Data is stale: last snapshot {info['age_s']:.0f}s ago during market hours.")
        elif (day == now.date().isoformat() and now.weekday() < 5 and now.time() > dt.time(9, 30)
              and not info["market_rows"]):
            flags.append("No market data recorded today (holiday, no login, or the stream is down).")
        if info["market_rows"] and not info["chain_rows"]:
            flags.append("Spot recorded but option chain empty: check expiry/instrument mapping in the bot logs.")
        info["events"] = dict(_q(con, "SELECT kind, COUNT(*) FROM events WHERE ts>=? AND ts<? GROUP BY kind", lo, hi))
        info["trades"] = _q(con, "SELECT ts, mode, variant, event, reason, net FROM trades "
                                 "WHERE ts>=? AND ts<? ORDER BY ts", lo, hi)
        s = _q(con, "SELECT COUNT(*), COALESCE(SUM(net),0), COALESCE(SUM(net>0),0) FROM shadow_trades "
                    "WHERE entry_ts>=? AND entry_ts<?", lo, hi)[0]
        info.update(shadow_closed=s[0], shadow_net=s[1], shadow_wins=s[2])
        if s[0]:
            for key, order in (("best", "DESC"), ("worst", "ASC")):
                info[key] = _q(con, f"SELECT variant, net FROM shadow_trades WHERE entry_ts>=? AND entry_ts<? "
                                    f"ORDER BY net {order} LIMIT 2", lo, hi)
        try:
            r = _q(con, "SELECT ts, pcr, call_wall, put_wall, max_pain, net_gex, atm_iv, skew, call_oi_chg, "
                        "put_oi_chg FROM features WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 1", lo, hi)
            info["features"] = r[0] if r else None
            info["feature_rows"] = _q(con, "SELECT COUNT(*) FROM features WHERE ts>=? AND ts<?", lo, hi)[0][0]
            info["flow_rows"] = _q(con, "SELECT COUNT(*) FROM flow WHERE ts>=? AND ts<?", lo, hi)[0][0]
        except sqlite3.OperationalError:          # database created by an older version
            info["features"], info["feature_rows"], info["flow_rows"] = None, 0, 0
        info["shadow_total"] = _q(con, "SELECT COUNT(*) FROM shadow_trades")[0][0]
        info["days"] = _q(con, "SELECT COUNT(DISTINCT substr(ts,1,10)) FROM market")[0][0]
    finally:
        con.close()
    return {"info": info, "flags": flags}


def sheet_row(now: dt.datetime, mode: str, res: dict[str, Any]) -> list:
    i, f = res["info"], res["flags"]
    return [now.isoformat(timespec="seconds"), mode, "yes" if i["token_fresh"] else "NO",
            i["paper_approved"] if mode == "paper" else i["live_approved"],
            i["spot"], i["vix"], i["regime"], i["market_rows"], i["chain_rows"],
            None if i["age_s"] is None else round(i["age_s"]), len(i["trades"]), i["shadow_closed"],
            round(i["shadow_net"]), "OK" if not f else " | ".join(f)[:300]]


HEALTH_HEADER = ["Time (IST)", "Mode", "Kite login", "Approved variants", "SENSEX spot", "India VIX", "Regime",
                 "Market rows today", "Chain rows today", "Data age (s)", "Trades today",
                 "Shadow trades closed", "Shadow net P&L", "Status / problems"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="IST date, default today")
    args = ap.parse_args()
    cfg = load_config()
    now = dt.datetime.now(IST)
    res = collect(cfg, now, args.date)
    i, flags = res["info"], res["flags"]
    print(f"=== SENSEX bot health | {now:%Y-%m-%d %H:%M} IST | report for {i['day']} ===")
    print("Kite token      :", "fresh" if i["token_fresh"] else "MISSING/EXPIRED")
    print(f"paper approvals : {i['paper_approved']} variants (valid until {i['paper_approved_until']})")
    print(f"live approvals  : {i['live_approved']} variants (valid until {i['live_approved_until']})")
    print(f"market snapshots: {i['market_rows']}  last {i['last_ts']}")
    print(f"chain rows      : {i['chain_rows']}")
    if i["spot"] is not None:
        print(f"latest market   : spot {i['spot']:.0f}  VIX {i['vix']}  regime {i['regime']}  "
              f"score {i['score']}  spike {i['spike']}")
    if i.get("features"):
        f = i["features"]
        fmt = lambda x, n=2: "n/a" if x is None else f"{x:,.{n}f}"
        print(f"OI structure    : PCR {fmt(f[1])}  call wall {fmt(f[2], 0)}  put wall {fmt(f[3], 0)}  "
              f"max pain {fmt(f[4], 0)}  net GEX {fmt(f[5], 0)}  ATM IV {fmt(f[6] and f[6] * 100, 1)}%  "
              f"skew {fmt(f[7], 1)}")
        print(f"                  5-min OI change: calls {fmt(f[8], 0)}  puts {fmt(f[9], 0)}  "
              f"| feature rows {i['feature_rows']}, tick-flow rows {i['flow_rows']}")
    print("events today    :", i["events"] or "none")
    print(f"real/paper trades today: {len(i['trades'])}")
    for t in i["trades"]:
        print("   ", t)
    print(f"shadow trades   : {i['shadow_closed']} closed today, net {i['shadow_net']:,.0f}, "
          f"winners {i['shadow_wins']}  (simulated for all variants)")
    for name in ("best", "worst"):
        if i[name]:
            print(f"   {name}:", [(v[:48], round(n)) for v, n in i[name]])
    print(f"history         : {i['days']} recorded day(s), {i['shadow_total']} shadow trades in total")
    if flags:
        print("\nATTENTION:")
        for f in flags:
            print(" -", f)
        return 1
    print("\nOK: no problems detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
