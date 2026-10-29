"""Walk-forward backtest of the intraday price-action family (templates ib_*) on 5-minute SENSEX + India VIX bars.

    python intraday_backtest.py --index data/sensex_5m.csv --vix data/vix_5m.csv [--out-dir data]
    python intraday_backtest.py --fetch-kite 1400            # download 5-minute history with your Kite session first

HOW IT WORKS (read before trusting a number)
  * Signals come from intraday.py, legs from strategy_library.build_plan: the same code the live bot runs.
  * Options are priced with Black-Scholes at ATM IV = India VIX (that 5-minute bar) x iv_scale with the put skew, on the
    backtest's trading-time clock (backtest.py). calibrate.py measured ATM IV / VIX = 0.951 on recorded chains.
  * Entries fill at the signal bar's close price +/- slippage (default 0.5% per fill; the recorded median half-spread
    is 0.13%). Every approval must also survive 2x slippage.
  * Exits, checked on every later bar: the plan's underlying bounds (stop / target, breakeven move after +1R) using
    the bar's high/low (if both bounds are touched in one bar the adverse one is assumed), the rupee hard stop of the
    exit engine, and the 15:15 square-off. Exits inside a bar are priced at the bound with that bar's VIX.
  * One lot (current SENSEX lot size, 20) per trade, at most `max_per_day` non-overlapping trades per variant per day.
  * Weekly expiry from the dated schedule in BacktestParams; 0-DTE excluded like the live bot.
  * Walk-forward approval uses backtest.walk_forward (rolling train / test windows) with the criteria below.
    Single-leg variants may be approved for every account tier; multi-leg ones only for medium / large accounts.
"""
from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import datetime as dt
import json
import logging
import math
import os
from multiprocessing import Pool

from backtest import Criteria, Trade, _years, expiry_for, model_chain, skew_iv, stats, walk_forward
from config import IST, BacktestParams, StrategyParams, load_config
from costs import CostParams, trade_cost
from exit_engine import ExitEngine, ExitParams
from intraday import Bar, key_levels, levels_from
from strategy_engine import Regime, RegimeState, bs_price
from strategy_library import IntradayContext, StrategySpec, build_library, build_plan

log = logging.getLogger("intraday_backtest")
SQUARE_OFF = dt.time(15, 15)
OR_BARS = 6


@dataclasses.dataclass(frozen=True)
class SimParams:
    slippage: float = 0.005
    iv_scale: float = 0.95
    skew: float = 1.5
    rf: float = 0.065
    max_per_day: int = 2
    lot_size: int = 20
    strike_step: float = 100.0


# ------------------------------------------------------------------ data
def load_5m(path: str) -> dict[str, list[Bar]]:
    days: dict[str, list[Bar]] = collections.defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = dt.datetime.fromisoformat(r["date"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            if not (h >= max(o, c) >= min(o, c) >= l > 0):
                continue
            days[ts.date().isoformat()].append(Bar(ts, o, h, l, c))
    return {d: sorted(b, key=lambda x: x.ts) for d, b in days.items()}


def load_vix(path: str) -> dict[dt.datetime, float]:
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = dt.datetime.fromisoformat(r["date"])
            out[ts if ts.tzinfo else ts.replace(tzinfo=IST)] = float(r["close"])
    return out


# ------------------------------------------------------------------ simulation
def _price(leg, spot: float, vix: float, when: dt.datetime, expiry: dt.date, sp: SimParams) -> float:
    T = max(_years(expiry, when), 1e-6)
    sig = skew_iv(vix / 100.0 * sp.iv_scale, spot, leg.contract.strike, sp.skew)
    return max(bs_price(spot, leg.contract.strike, T, sp.rf, sig, leg.contract.kind), 0.05)


def _pnl(plan, entry: dict, spot: float, vix: float, when: dt.datetime, sp: SimParams, slip: bool):
    """Mark-to-market (slip=False) or executable exit (slip=True) P&L of one lot, plus the exit fills."""
    total, fills = 0.0, []
    for leg in plan.legs:
        px = _price(leg, spot, vix, when, plan.expiry, sp)
        side = "SELL" if leg.side == "BUY" else "BUY"
        if slip:
            px *= (1 - sp.slippage) if side == "SELL" else (1 + sp.slippage)
        q = plan.qty_for(leg)
        total += (1 if leg.side == "BUY" else -1) * (px - entry[leg.contract.symbol]) * q
        fills.append((side, px, q))
    return total, fills


def _simulate(plan, bars: list[Bar], i: int, vix: list[float], sp: SimParams, costs: CostParams,
              stop_rs: float) -> tuple[Trade, int]:
    """Enter at bars[i].close, manage on bars[i+1:], return (trade, index of the exit bar)."""
    entry = {}
    for leg in plan.legs:
        px = leg.ref_price * (1 + sp.slippage if leg.side == "BUY" else 1 - sp.slippage)
        entry[leg.contract.symbol] = px
    lo, hi, be = plan.spot_lo, plan.spot_hi, plan.spot_be
    e_spot = plan.entry_spot
    reason, x_spot, x_when, x_vix, j = "SQUARE_OFF_TIME", bars[-1].close, bars[-1].ts + dt.timedelta(minutes=5), vix[-1], len(bars) - 1
    for j in range(i + 1, len(bars)):
        b, v = bars[j], vix[j]
        mid = b.ts + dt.timedelta(minutes=2.5)
        hits = []
        if lo is not None and b.low <= lo:
            hits.append((min(b.open, lo), "SPOT_LO"))
        if hi is not None and b.high >= hi:
            hits.append((max(b.open, hi), "SPOT_HI"))
        worst_spot = None
        if hits:                                     # two bounds in one bar: assume the worse one happened
            worst_spot, why = min(hits, key=lambda h: _pnl(plan, entry, h[0], v, mid, sp, False)[0])
            reason, x_spot, x_when, x_vix = why, worst_spot, mid, v
            break
        if stop_rs > 0:
            w = min(_pnl(plan, entry, s, v, mid, sp, False)[0] for s in (b.low, b.high))
            if w <= -stop_rs:
                reason, x_spot, x_when, x_vix = "HARD_STOP", (b.low if _pnl(plan, entry, b.low, v, mid, sp, False)[0] == w
                                                              else b.high), mid, v
                break
        if (b.ts + dt.timedelta(minutes=5)).time() >= SQUARE_OFF:
            reason, x_spot, x_when, x_vix = "SQUARE_OFF_TIME", b.close, b.ts + dt.timedelta(minutes=5), v
            break
        if be is not None and e_spot is not None:   # breakeven move after +1R (applies from the next bar)
            if be > e_spot and b.high >= be:
                lo, be = max(lo if lo is not None else -math.inf, e_spot), None
            elif be < e_spot and b.low <= be:
                hi, be = min(hi if hi is not None else math.inf, e_spot), None
    gross, xfills = _pnl(plan, entry, x_spot, x_vix, x_when, sp, True)
    fills = [(l.side, entry[l.contract.symbol], plan.qty_for(l)) for l in plan.legs] + xfills
    net = gross - trade_cost(fills, costs)
    return Trade(bars[i].ts.date(), net, plan.max_loss_per_lot, reason, gross, tuple(fills)), j


def run_days(args) -> dict[str, list[Trade]]:
    """Simulate intraday specs over a block of days. args: (day keys, all days, vix map, specs, SimParams)."""
    keys, days, vixmap, specs, sp = args
    bp = dataclasses.replace(BacktestParams(), lot_size=sp.lot_size, strike_step=sp.strike_step)
    costs = CostParams()
    xe = ExitEngine(ExitParams(), verbose=False)
    order = sorted(days)
    pos = {d: k for k, d in enumerate(order)}
    out: dict[str, list[Trade]] = {s.id: [] for s in specs}
    for d in keys:
        k = pos[d]
        if k < 10:
            continue
        bars = days[d]
        if len(bars) < 40:
            continue
        prev = [days[order[x]] for x in range(k - 10, k)]
        lv = levels_from(prev, bars, OR_BARS)
        if lv is None:
            continue
        day = bars[0].ts.date()
        expiry = expiry_for(day, bp)
        dte = (expiry - day).days
        vix, last = [], None
        for b in bars:
            last = vixmap.get(b.ts, last)
            vix.append(last if last else 15.0)
        kl = [key_levels(lv, bars[:i]) for i in range(len(bars) + 1)]
        busy_until = {s.id: -1 for s in specs}
        count = {s.id: 0 for s in specs}
        for i in range(OR_BARS, len(bars) - 1):
            now = bars[i].ts + dt.timedelta(minutes=5)
            if now.time() >= SQUARE_OFF:
                break
            spike = i >= 3 and vix[i - 3] > 0 and vix[i] / vix[i - 3] - 1 >= 0.10
            rs = RegimeState(Regime.NORMAL, vix[i], 0.0, 0.0, spike, 0)
            ictx = IntradayContext(bars[:i + 1], lv, kl)
            chain = None
            for s in specs:
                if busy_until[s.id] >= i or count[s.id] >= sp.max_per_day or not (s.dte_min <= dte <= s.dte_max):
                    continue
                if ictx.signal(s) is None:
                    continue
                if chain is None:
                    chain = model_chain(bars[i].close, vix[i] / 100 * sp.iv_scale, expiry, now, bp, sp.rf)
                plan = build_plan(s, chain, rs, None, day, now, None, None, ictx)
                if plan is None:
                    continue
                stop_rs = xe.stop_for(plan.max_loss_per_lot, plan.sl_frac)
                trade, j = _simulate(plan, bars, i, vix, sp, costs, stop_rs)
                out[s.id].append(trade)
                busy_until[s.id], count[s.id] = j, count[s.id] + 1
    return out


def run(days: dict[str, list[Bar]], vixmap, specs: list[StrategySpec], sp: SimParams, workers: int) -> dict[str, list[Trade]]:
    keys = sorted(days)
    n = max(workers, 1)
    chunks = [keys[i::n] for i in range(n)]
    jobs = [(c, days, vixmap, list(specs), sp) for c in chunks]
    if n == 1:
        parts = [run_days(jobs[0])]
    else:
        with Pool(n) as pool:
            parts = pool.map(run_days, jobs)
    out: dict[str, list[Trade]] = {s.id: [] for s in specs}
    for part in parts:
        for vid, trades in part.items():
            out[vid].extend(trades)
    for vid in out:
        out[vid].sort(key=lambda t: t.date)
    return out


# ------------------------------------------------------------------ report
def family(vid: str) -> str:
    t, p = vid.split("|")[:2]
    sig = {"1": "breakout", "2": "trap", "3": "range", "4": "pullback"}.get(p.split("_")[0][3:], "?")
    return f"{t}/{sig}"


def fetch_kite(days: int, out_dir: str) -> tuple[str, str]:
    from zerodha_client import ZerodhaClient
    cfg = load_config()
    client = ZerodhaClient(cfg.credentials, cfg.paths.token_file)
    client.authenticate()
    client.load_instruments()
    end = dt.datetime.now(IST)
    paths = []
    for name, tok in (("sensex_5m.csv", client.spot_token), ("vix_5m.csv", client.vix_token)):
        rows, start = {}, end - dt.timedelta(days=days)
        while start < end:
            stop = min(start + dt.timedelta(days=95), end)          # Kite: at most 100 days of 5-minute bars a call
            for r in client.historical(tok, start, stop, "5minute"):
                rows[r["date"]] = r
            start = stop + dt.timedelta(minutes=1)
        p = os.path.join(out_dir, name)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close"])
            for d in sorted(rows):
                r = rows[d]
                w.writerow([d.isoformat(), r["open"], r["high"], r["low"], r["close"]])
        paths.append(p)
        log.info("wrote %s (%d bars)", p, len(rows))
    return paths[0], paths[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--index", help="SENSEX 5-minute CSV (date,open,high,low,close)")
    src.add_argument("--fetch-kite", type=int, metavar="DAYS")
    ap.add_argument("--vix", help="India VIX 5-minute CSV")
    ap.add_argument("--out-dir", default=os.getenv("DATA_DIR", "data"))
    ap.add_argument("--slippage", type=float, default=0.005)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--valid-days", type=int, default=60)
    ap.add_argument("--train-days", type=int, default=250)
    ap.add_argument("--test-days", type=int, default=60)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.fetch_kite:
        index, vixp = fetch_kite(args.fetch_kite, args.out_dir)
    else:
        if not args.vix:
            ap.error("--index requires --vix")
        index, vixp = args.index, args.vix
    days, vixmap = load_5m(index), load_vix(vixp)
    bp = BacktestParams()
    sp = SimParams(slippage=args.slippage, iv_scale=bp.iv_scale, skew=bp.skew_slope, rf=StrategyParams().risk_free_rate)
    specs = [s for s in build_library() if s.intraday_signal]
    log.info("Intraday backtest: %d variants over %d days (%s to %s)", len(specs), len(days), min(days), max(days))
    results = run(days, vixmap, specs, sp, args.workers)
    dates = sorted(dt.date.fromisoformat(d) for d in days)[10:]
    c = Criteria(min_train_trades=15, train_pf=1.1, min_trades=60, pf=1.2, min_oos_trades=30, oos_pf=1.1,
                 pos_window_frac=0.6, dd_mult=10.0, train_days=args.train_days, test_days=args.test_days)
    wf_rows, per = walk_forward(results, dates, c)
    stress = run(days, vixmap, specs, dataclasses.replace(sp, slippage=2 * sp.slippage), args.workers)
    for vid, v in per.items():
        st = stats(stress[vid])
        v["stress"] = st
        if v["approved"] and not (st["net"] > 0 and st["pf"] >= 1.0):
            v["approved"], v["score"] = False, 0.0
    by_id = {s.id: s for s in specs}

    print(f"\n{'family':<22} {'n':>6} {'net':>11} {'pf':>5} {'win%':>5}   OOS: {'n':>5} {'net':>10} {'pf':>5}")
    fam_full: dict[str, list[Trade]] = collections.defaultdict(list)
    fam_oos: dict[str, list] = collections.defaultdict(list)
    for vid, v in per.items():
        fam_full[family(vid)] += results[vid]
        fam_oos[family(vid)].append(v["oos"])
    for f in sorted(fam_full):
        s = stats(fam_full[f])
        on = sum(o["n"] for o in fam_oos[f])
        onet = sum(o["net"] for o in fam_oos[f])
        print(f"{f:<22} {s['n']:>6} {s['net']:>11,.0f} {s['pf']:>5.2f} {100 * s['win_rate']:>5.0f}   "
              f"     {on:>5} {onet:>10,.0f}")
    if wf_rows:
        avg = sum(r["oos_net_per_chosen"] for r in wf_rows) / len(wf_rows)
        print(f"\nWalk-forward windows: {len(wf_rows)}; honest OOS estimate (avg net per chosen variant per test "
              f"window, per lot, after costs): {avg:,.0f}")
    approved = sorted((v for v in per.values() if v["approved"]), key=lambda v: -v["score"])
    print(f"Approved {len(approved)} of {len(specs)} intraday variants")
    for v in approved[:20]:
        f, o = v["full"], v["oos"]
        print(f"  {v['id']:<66} n={f['n']:<4} net={f['net']:>9,.0f} pf={f['pf']:.2f} oosPF={o['pf']:.2f} "
              f"stressPF={v['stress']['pf']:.2f} wins={v['pos_windows']}")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "intraday_report.json"), "w", encoding="utf-8") as fh:
        json.dump({"windows": wf_rows, "variants": list(per.values())}, fh, default=str, indent=1)
    doc = {"generated_at": dt.datetime.now(IST).isoformat(), "source": "intraday_backtest",
           "data_range": [min(days), max(days)], "slippage": sp.slippage,
           "valid_until": str(dt.date.today() + dt.timedelta(days=args.valid_days)),
           "variants": [{"id": v["id"], "score": v["score"], "full": v["full"], "oos": v["oos"],
                         "tiers": ["medium", "large"] if by_id[v["id"]].multi_leg else ["small", "medium", "large"]}
                        for v in approved]}
    target = os.path.join(args.out_dir, "approved_intraday.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"\nWrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
