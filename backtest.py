"""Walk-forward backtest of the whole strategy library -> data/approved_backtest.json.

HOW IT WORKS (and its limits; read before trusting any result)
  * Kite does not serve historical prices of EXPIRED option contracts, so option
    prices are MODELLED: Black-Scholes with ATM IV = India VIX * iv_scale and a
    put-side skew (see BacktestParams in config.py).
  * Daily bars: one trade per variant per day, entered at the open and exited
    by a stop (tested at both intraday extremes, assumed to hit before any
    target) or by a target/close mark (a target only counts if the CLOSE mark
    reaches it). This is deliberately pessimistic for BOTH sellers and buyers.
    Trailing stops and the true intraday path are not modelled.
  * Costs (brokerage, STT 0.15% on sold premium, charges) and slippage are
    included. Weekly-expiry weekday, strike step and lot size are assumptions
    in BacktestParams: verify them.
  * A variant is approved only if it passes the full-sample AND out-of-sample
    (walk-forward test windows) criteria below. With ~160 correlated variants
    some will pass by luck; that is why promote.py also demands forward
    (shadow) evidence before anything trades live.

Usage:
  python backtest.py --fetch-kite 1500          # download SENSEX + India VIX daily via your Kite session, then run
  python backtest.py --index data/sensex_daily.csv --vix data/vix_daily.csv
  python backtest.py --synthetic                # harness self-test on fake data; NEVER writes approvals
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import random
from dataclasses import dataclass
from typing import Optional

from config import IST, TIER_CAPITAL, BacktestParams, RiskParams, Schedule, StrategyParams, load_config
from costs import CostParams, trade_cost
from strategy_engine import (Chain, OptionQuote, RegimeDetector, bs_greeks, bs_price,
                             trend_direction)
from trend_regime import TrendInfo, daily_state
from strategy_library import StrategySpec, build_library, build_plan
from zerodha_client import OptionContract

log = logging.getLogger("backtest")
TICK = 0.05


# ------------------------------------------------------------------ model chain
def skew_iv(iv_atm: float, spot: float, strike: float, slope: float) -> float:
    sig = iv_atm * (1.0 - slope * math.log(strike / spot))
    return min(max(sig, 0.5 * iv_atm), 2.5 * iv_atm)


SESSION_HOURS = 6.25      # 09:15-15:30


def _years(expiry: dt.date, now: dt.datetime) -> float:
    """Time to expiry on a TRADING-TIME clock (252 days/yr, weekends skipped, only
    session hours count within a day).

    Why not calendar time: the backtest feeds daily bars, so a full trading day's
    realised variance arrives inside ~6 session hours. Pricing with a calendar
    clock would decay options by only 6/24 of a day over that span, which hands
    option BUYERS a large phantom edge (and punishes sellers). Using the same
    clock for implied and realised variance removes that bias; ``iv_scale`` then
    controls the variance risk premium (IV vs realised vol).
    """
    close = dt.datetime.combine(now.date(), dt.time(15, 30), tzinfo=IST)
    frac_today = min(max((close - now).total_seconds() / (SESSION_HOURS * 3600), 0.0), 1.0)
    days, d = 0, now.date()
    while d < expiry:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return max(frac_today + days, 1e-3) / 252.0


def model_chain(spot: float, iv_atm: float, expiry: dt.date, now: dt.datetime,
                bp: BacktestParams, rf: float) -> Chain:
    step = bp.strike_step
    atm = round(spot / step) * step
    T = _years(expiry, now)
    chain = Chain(expiry, spot, atm, step, 1.0)
    for i in range(-bp.window, bp.window + 1):
        k = atm + i * step
        sig = skew_iv(iv_atm, spot, k, bp.skew_slope)
        for kind in ("CE", "PE"):
            px = round(bs_price(spot, k, T, rf, sig, kind) / TICK) * TICK
            if px < 0.5:            # no realistic quote this far out of the money
                continue
            c = OptionContract(0, f"SX{expiry:%y%m%d}{int(k)}{kind}", "BFO", k, expiry, kind,
                               bp.lot_size, TICK)
            chain.quotes[(k, kind)] = OptionQuote(c, px, 1.0, sig, bs_greeks(spot, k, T, rf, sig, kind))
    return chain


# ------------------------------------------------------------------ simulation
@dataclass(frozen=True)
class Trade:
    date: dt.date
    net: float
    max_loss: float
    reason: str
    gross: float = 0.0                  # per ONE lot, before costs
    fills: tuple = ()                   # ((side, price, qty), ...) for one lot, to re-cost n lots


def expiry_weekday_on(day: dt.date, bp: BacktestParams) -> int:
    """Weekly-expiry weekday in force on `day`: a forced weekday if configured, else the dated schedule."""
    if bp.expiry_weekday >= 0:
        return bp.expiry_weekday
    wd = bp.expiry_schedule[0][1]
    for start, weekday in bp.expiry_schedule:
        if day >= start:
            wd = weekday
    return wd


def next_expiry(day: dt.date, weekday: int) -> dt.date:
    delta = (weekday - day.weekday()) % 7
    return day + dt.timedelta(days=delta or 7)          # 0-DTE excluded, like the live bot


def simulate_trade(plan, spec: StrategySpec, bar: dict, iv_entry: float, iv_exit: float,
                   bp: BacktestParams, costs: CostParams, rf: float, entry_dt: dt.datetime) -> Trade:
    slip = bp.slippage_pct
    qtys = {l.contract.symbol: plan.lot_size * l.ratio for l in plan.legs}
    entry = {}
    for leg in plan.legs:
        px = leg.ref_price * (1 + slip if leg.side == "BUY" else 1 - slip)
        entry[leg.contract.symbol] = round(px / TICK) * TICK

    def leg_px(leg, spot, iv, hours) -> float:
        T = max(_years(plan.expiry, entry_dt + dt.timedelta(hours=hours)), 1e-6)
        sig = skew_iv(iv, spot, leg.contract.strike, bp.skew_slope)
        return max(bs_price(spot, leg.contract.strike, T, rf, sig, leg.contract.kind), TICK)

    def pnl_at(spot, iv, hours) -> tuple[float, list[tuple[str, float, int]]]:
        total, exits = 0.0, []
        for leg in plan.legs:
            side_exit = "SELL" if leg.side == "BUY" else "BUY"
            px = leg_px(leg, spot, iv, hours)
            px *= (1 - slip) if side_exit == "SELL" else (1 + slip)
            sign = 1 if leg.side == "BUY" else -1
            qty = qtys[leg.contract.symbol]
            total += sign * (px - entry[leg.contract.symbol]) * qty
            exits.append((side_exit, px, qty))
        return total, exits

    ml, mp = plan.max_loss_per_lot, plan.max_profit_per_lot
    sl, tp = spec.sl_frac * ml, spec.tp_frac * mp
    # Pessimistic daily-bar rules, symmetric for sellers and buyers:
    #  * the stop is tested at BOTH intraday extremes and assumed to hit before any target;
    #  * a target only counts if the CLOSE mark reaches it (touching it intraday and
    #    reversing is ignored, so neither side gets a free option on the day's extreme).
    p_hi, ex_hi = pnl_at(bar["high"], iv_exit, 3.0)
    p_lo, ex_lo = pnl_at(bar["low"], iv_exit, 3.0)
    worst, w_ex = (p_hi, ex_hi) if p_hi <= p_lo else (p_lo, ex_lo)
    if worst <= -sl:
        gross, exits, reason = -min(sl * (1.0 + bp.stop_overshoot), ml), w_ex, "SL"   # stops fill worse than the trigger
    else:
        p_close, c_ex = pnl_at(bar["close"], iv_exit, 5.75)
        if p_close >= tp:
            gross, exits, reason = tp, c_ex, "TP"
        else:
            gross, exits, reason = p_close, c_ex, "EOD"
    fills = [(l.side, entry[l.contract.symbol], qtys[l.contract.symbol]) for l in plan.legs] + exits
    return Trade(bar["date"], gross - trade_cost(fills, costs), ml, reason, gross, tuple(fills))


def run_backtest(bars: list[dict], specs: list[StrategySpec], sp: StrategyParams,
                 bp: BacktestParams, costs: CostParams, warmup: int = 30) -> dict[str, list[Trade]]:
    results: dict[str, list[Trade]] = {s.id: [] for s in specs}
    det = RegimeDetector(sp)
    candles: list[dict] = []
    for i, bar in enumerate(bars):
        if i >= warmup:
            vix_prev = bars[i - 1]["vix"]
            rs = det.update(vix_prev, candles[-60:], t=i * 86400.0)
            expiry = next_expiry(bar["date"], expiry_weekday_on(bar["date"], bp))
            if sp.min_dte <= (expiry - bar["date"]).days <= sp.max_dte:
                now = dt.datetime.combine(bar["date"], dt.time(9, 30), tzinfo=IST)
                iv0, iv1 = vix_prev / 100 * bp.iv_scale, bar["vix"] / 100 * bp.iv_scale
                tdir = trend_direction([c["close"] for c in candles[-60:]], sp)   # same input as the live bot
                tinfo = TrendInfo(daily_state(candles[-60:]))
                chain = model_chain(bar["open"], iv0, expiry, now, bp, sp.risk_free_rate)
                for spec in specs:
                    if spec.forward_only:
                        continue                       # no historical OI / 5-minute data: validated forward only
                    plan = build_plan(spec, chain, rs, tdir, bar["date"], now, None, tinfo)
                    if plan:
                        results[spec.id].append(
                            simulate_trade(plan, spec, bar, iv0, iv1, bp, costs, sp.risk_free_rate, now))
        candles.append(bar)
    return results


def tier_results(results: dict[str, list[Trade]], capital: float, rp, schedule, costs: CostParams,
                 profile: str = "auto") -> dict[str, list[Trade]]:
    """Re-express one-lot results for an account of `capital`: lots come from the real RiskManager sizing rule, costs
    are recomputed for n lots, and trades that do not fit the account are dropped."""
    from types import SimpleNamespace
    from risk_manager import RiskManager
    rm = RiskManager(rp, schedule)
    rm.apply_capital(capital, profile)
    out: dict[str, list[Trade]] = {}
    for vid, trades in results.items():
        rows = []
        for t in trades:
            n = rm.size(SimpleNamespace(max_loss_per_lot=t.max_loss), capital)
            if n < 1:
                continue
            fills = [(s, px, q * n) for s, px, q in t.fills]
            rows.append(Trade(t.date, n * t.gross - trade_cost(fills, costs), n * t.max_loss, t.reason,
                              n * t.gross, tuple(fills)))
        out[vid] = rows
    return out


# ------------------------------------------------------------------ statistics
def stats(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "net": 0.0, "avg": 0.0, "win_rate": 0.0, "pf": 0.0, "max_dd": 0.0, "avg_max_loss": 0.0}
    wins = sum(t.net for t in trades if t.net > 0)
    losses = -sum(t.net for t in trades if t.net < 0)
    peak = cum = dd = 0.0
    for t in sorted(trades, key=lambda x: x.date):
        cum += t.net
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "net": sum(t.net for t in trades), "avg": sum(t.net for t in trades) / n,
            "win_rate": sum(1 for t in trades if t.net > 0) / n,
            "pf": 99.0 if losses == 0 and wins > 0 else (wins / losses if losses else 0.0),
            "max_dd": dd, "avg_max_loss": sum(t.max_loss for t in trades) / n}


@dataclass(frozen=True)
class Criteria:
    min_train_trades: int = 10
    train_pf: float = 1.1
    min_trades: int = 50
    pf: float = 1.3
    min_oos_trades: int = 25
    oos_pf: float = 1.2
    pos_window_frac: float = 0.6
    dd_mult: float = 10.0          # max drawdown <= dd_mult * average max-loss per trade
    train_days: int = 120
    test_days: int = 40


def walk_forward(results: dict[str, list[Trade]], dates: list[dt.date], c: Criteria) -> tuple[list[dict], dict]:
    idx = {d: i for i, d in enumerate(dates)}
    windows, start = [], 0
    while start + c.train_days + c.test_days <= len(dates):
        tr = (start, start + c.train_days)
        windows.append((tr, (tr[1], tr[1] + c.test_days)))
        start += c.test_days
    wf_rows = []
    for tr, te in windows:
        chosen, oos = 0, 0.0
        for trades in results.values():
            s = stats([t for t in trades if tr[0] <= idx[t.date] < tr[1]])
            if s["n"] >= c.min_train_trades and s["pf"] >= c.train_pf and s["net"] > 0:
                chosen += 1
                oos += sum(t.net for t in trades if te[0] <= idx[t.date] < te[1])
        wf_rows.append({"test_start": str(dates[te[0]]), "chosen": chosen,
                        "oos_net_per_chosen": (oos / chosen) if chosen else 0.0})
    per_variant: dict[str, dict] = {}
    for vid, trades in results.items():
        oos_trades = [t for t in trades if any(te[0] <= idx[t.date] < te[1] for _, te in windows)]
        pos = tot = 0
        for _, te in windows:
            w = [t for t in trades if te[0] <= idx[t.date] < te[1]]
            if len(w) >= 3:
                tot += 1
                pos += 1 if sum(t.net for t in w) > 0 else 0
        full, oos_s = stats(trades), stats(oos_trades)
        ok = (full["n"] >= c.min_trades and full["pf"] >= c.pf and full["net"] > 0
              and oos_s["n"] >= c.min_oos_trades and oos_s["pf"] >= c.oos_pf and oos_s["net"] > 0
              and tot > 0 and pos / tot >= c.pos_window_frac
              and full["max_dd"] <= c.dd_mult * max(full["avg_max_loss"], 1.0))
        per_variant[vid] = {"id": vid, "full": full, "oos": oos_s,
                            "pos_windows": f"{pos}/{tot}", "approved": ok,
                            "score": (full["net"] / max(full["max_dd"], full["avg_max_loss"], 1.0)) if ok else 0.0}
    return wf_rows, per_variant


# ------------------------------------------------------------------ data loading
def load_csv(index_csv: str, vix_csv: str) -> list[dict]:
    def read(path: str) -> dict[dt.date, dict]:
        out = {}
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[dt.date.fromisoformat(r["date"][:10])] = {k: float(v) for k, v in r.items() if k != "date"}
        return out
    idx, vix = read(index_csv), read(vix_csv)
    bars, bad = [], 0
    for d, r in sorted(idx.items()):
        if d not in vix:
            continue
        o, h, l, c, v = r["open"], r["high"], r["low"], r["close"], vix[d]["close"]
        vals = (o, h, l, c, v)
        if any(x != x or x <= 0 for x in vals) or h < l or not (l <= o <= h) or not (l <= c <= h) or v > 150:
            bad += 1                                  # zero/NaN/inconsistent bars would corrupt prices and IV
            continue
        bars.append({"date": d, "open": o, "high": h, "low": l, "close": c, "vix": v})
    if bad:
        log.warning("Dropped %d invalid bar(s) from the input data", bad)
    jumps = [bars[i]["date"] for i in range(1, len(bars)) if abs(bars[i]["open"] / bars[i - 1]["close"] - 1) > 0.10]
    if jumps:
        log.warning("Gaps > 10%% between close and next open on %s: check for splits or bad data", jumps[:5])
    if len(bars) < 300:
        raise SystemExit(f"Only {len(bars)} aligned days; need at least ~300 for a meaningful walk-forward")
    return bars


def fetch_kite(days: int, out_dir: str) -> tuple[str, str]:
    from zerodha_client import ZerodhaClient     # imported lazily: needs kiteconnect + a valid token
    cfg = load_config()
    client = ZerodhaClient(cfg.credentials, cfg.paths.token_file)
    client.authenticate()
    client.load_instruments()
    end = dt.datetime.now(IST)
    start = end - dt.timedelta(days=days)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for name, token in (("sensex_daily.csv", client.spot_token), ("vix_daily.csv", client.vix_token)):
        rows = client.historical(token, start, end, "day")
        p = os.path.join(out_dir, name)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close"])
            for r in rows:
                w.writerow([r["date"].date().isoformat(), r["open"], r["high"], r["low"], r["close"]])
        paths.append(p)
        log.info("wrote %s (%d rows)", p, len(rows))
    return paths[0], paths[1]


def synthetic_bars(n: int = 1200, seed: int = 7) -> list[dict]:
    """Fake regime-switching data for harness testing only."""
    rnd = random.Random(seed)
    price, vol, day = 70000.0, 0.13, dt.date(2020, 1, 1)
    bars = []
    while len(bars) < n:
        day += dt.timedelta(days=1)
        if day.weekday() >= 5:
            continue
        if rnd.random() < 0.03:
            vol = rnd.choice([0.09, 0.12, 0.16, 0.24])
        daily = vol / math.sqrt(252)
        o = price * (1 + rnd.gauss(0, daily * 0.3))
        c = o * (1 + rnd.gauss(0.0003, daily))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, daily * 0.4)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, daily * 0.4)))
        bars.append({"date": day, "open": o, "high": hi, "low": lo, "close": c,
                     "vix": max(8.0, vol * 100 * (1 + rnd.gauss(0, 0.08)) + 2.0)})
        price = c
    return bars


# ------------------------------------------------------------------ entry point
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fetch-kite", type=int, metavar="DAYS")
    src.add_argument("--index", help="SENSEX daily CSV (date,open,high,low,close)")
    src.add_argument("--synthetic", action="store_true")
    ap.add_argument("--vix", help="India VIX daily CSV (date,open,high,low,close)")
    ap.add_argument("--out-dir", default=os.getenv("DATA_DIR", "data"))
    ap.add_argument("--valid-days", type=int, default=60, help="how long the approvals stay valid")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = load_config() if not args.synthetic else None
    sp = cfg.strategy if cfg else StrategyParams()
    bp = cfg.backtest if cfg else BacktestParams()
    costs = cfg.costs if cfg else CostParams()

    if args.synthetic:
        bars = synthetic_bars()
    elif args.fetch_kite:
        bars = load_csv(*fetch_kite(args.fetch_kite, args.out_dir))
    else:
        if not args.vix:
            ap.error("--index requires --vix")
        bars = load_csv(args.index, args.vix)

    specs = build_library()
    log.info("Backtesting %d variants over %d days (%s to %s)", len(specs), len(bars),
             bars[0]["date"], bars[-1]["date"])
    results = run_backtest(bars, specs, sp, bp, costs)
    dates = [b["date"] for b in bars[30:]]
    c = Criteria()
    wf_rows, per_variant = walk_forward(results, dates, c)
    # Stress gate: the model's fills are the weakest assumption, so an approval must also survive DOUBLE slippage.
    import dataclasses
    stress = run_backtest(bars, specs, sp, dataclasses.replace(bp, slippage_pct=2 * bp.slippage_pct), costs)
    stress_ok = {vid: (lambda s: s["net"] > 0 and s["pf"] >= 1.0)(stats(tr)) for vid, tr in stress.items()}
    dropped = [vid for vid, v in per_variant.items() if v["approved"] and not stress_ok.get(vid, False)]
    for vid in dropped:
        per_variant[vid]["approved"], per_variant[vid]["score"] = False, 0.0
    print(f"Stress test (2x slippage): {len(dropped)} otherwise-approved variants dropped")
    approved = sorted((v for v in per_variant.values() if v["approved"]), key=lambda v: -v["score"])
    # ---- capital tiers: same trades, sized and costed for a small / medium / large account
    rp = cfg.risk if cfg else RiskParams()
    sched = cfg.schedule if cfg else Schedule()
    tiers_ok: dict[str, dict[str, dict]] = {}
    for tier, cap in TIER_CAPITAL.items():
        _, pv = walk_forward(tier_results(results, cap, rp, sched, costs), dates, c)
        for vid in dropped:
            if vid in pv:
                pv[vid]["approved"], pv[vid]["score"] = False, 0.0
        tiers_ok[tier] = pv
        print(f"Tier {tier:<6} (Rs {cap:>9,.0f}): approved {sum(1 for v in pv.values() if v['approved'])} variants")
    for v in approved:
        v["tiers"] = {t: {"approved": pv[v["id"]]["approved"], "full": pv[v["id"]]["full"],
                          "oos": pv[v["id"]]["oos"]} for t, pv in tiers_ok.items()}
    # a variant can fail the base (1-lot) gates yet pass for a tier, e.g. a wider spread sized to 2 lots: include it
    base_ids = {v["id"] for v in approved}
    for tier, pv in tiers_ok.items():
        for vid, v in pv.items():
            if v["approved"] and vid not in base_ids:
                approved.append({**v, "tiers": {t: {"approved": p[vid]["approved"], "full": p[vid]["full"],
                                                   "oos": p[vid]["oos"]} for t, p in tiers_ok.items()}})
                base_ids.add(vid)
    approved.sort(key=lambda v: -v["score"])

    print(f"\nWalk-forward windows: {len(wf_rows)}")
    if wf_rows:
        avg = sum(r["oos_net_per_chosen"] for r in wf_rows) / len(wf_rows)
        print(f"Honest OOS estimate: avg net per chosen variant per test window = {avg:,.0f} (per lot, after costs)")
    print(f"Approved {len(approved)} of {len(specs)} variants")
    for v in approved[:15]:
        f, o = v["full"], v["oos"]
        print(f"  {v['id']:<62} n={f['n']:<4} net={f['net']:>9,.0f} pf={f['pf']:.2f} "
              f"oosPF={o['pf']:.2f} dd={f['max_dd']:>8,.0f} wins={v['pos_windows']}")

    tag = "synthetic_" if args.synthetic else ""
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, f"{tag}backtest_report.json"), "w", encoding="utf-8") as f:
        json.dump({"windows": wf_rows, "variants": list(per_variant.values())}, f, default=str, indent=1)
    doc = {"generated_at": dt.datetime.now(IST).isoformat(), "source": "backtest" + ("-SYNTHETIC" if args.synthetic else ""),
           "data_range": [str(bars[0]["date"]), str(bars[-1]["date"])],
           "valid_until": str(dt.date.today() + dt.timedelta(days=args.valid_days)),
           "variants": [{"id": v["id"], "score": v["score"], "full": v["full"], "oos": v["oos"],
                        "tiers": [t for t, r in v["tiers"].items() if r["approved"]],
                        "tier_stats": v["tiers"]} for v in approved]}
    target = os.path.join(args.out_dir, f"{tag}approved_backtest.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"\nWrote {target}")
    if args.synthetic:
        print("SYNTHETIC DATA: results above are meaningless; this only proves the pipeline runs. "
              "The bot reads approved_backtest.json, which was NOT touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
