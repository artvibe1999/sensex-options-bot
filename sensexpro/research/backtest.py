"""Underlying-signal research (brief section 9, layer 1). THIS IS NOT AN OPTIONS P&L BACKTEST.

Kite serves no history for expired option contracts (and no intraday history for expired futures, so no historical
volume either). What this measures: the live strategy, regime, ensemble and adaptive-exit code replayed on historical
5-minute INDEX candles, one position at a time, max 3 entries a day, the same cool-down and loss-streak rules; the
outcome of each trade in INDEX POINTS. Option liquidity points are assumed (config: --liquidity-pts) and option-health
inputs are missing (scored at half weight, as live when unavailable).

A rough option-equivalent estimate is reported separately and labelled as a MODEL: points x delta 0.5 x lot size,
minus the ATM option's time decay over the holding time (premium from India VIX, fixed days-to-expiry assumption),
minus the spread and Zerodha charges. Use it only to see whether the points clear the costs of buying options.

Look-ahead: one Frame is built per session from that session and the 5 before it; every series is causal (value i
uses bars <= i; 15-minute context at i is the last 15-minute bar closed by bar i) and the test-suite checks that
strategy outputs at bar i are identical on a frame truncated at i.

    python -m sensexpro.research.backtest --csv data/hist/NIFTY_5m.csv --index NIFTY --vix data/hist/INDIAVIX_5m.csv \
        --out research_out/NIFTY.json [--no-volume-filter]
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import math
import os
import sys
from collections import defaultdict
from typing import Optional

from costs import CostParams, trade_cost

from .. import candles as cd
from ..exits import EXIT, REDUCE, AdaptiveExitEngine, OptionTracker, update_structure
from ..features import Frame
from ..models import IST, Bar, ContractMeta, Position
from ..regime import detect
from ..settings import Settings, load_settings
from ..strategies.ensemble import ELIGIBLE, Ensemble
from .metrics import summarize

LOTS = {"SENSEX": 20, "NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "NIFTYNXT50": 25}
EXCH = {"SENSEX": "BFO"}
DTE_ASSUMED = {"SENSEX": 3, "NIFTY": 3}                 # weeklies; monthly-only indices: 10 trading days


def load_bars(path: str, constant_volume: Optional[float]) -> list[Bar]:
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = dt.datetime.fromisoformat(r["ts"]).replace(tzinfo=IST)
            try:
                out.append(Bar(t, 5, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                               constant_volume))
            except ValueError:
                continue
    return out


def load_vix(path: Optional[str]) -> dict:
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["ts"][:10]] = float(r["open"])           # the session's opening VIX (known at entry time)
    return out


def agg15_from5(bars5: list[Bar]) -> list[Bar]:
    """15-minute bars from 5-minute bars, aligned to 09:15, complete triples only (or session end)."""
    out, groups = [], defaultdict(list)
    order = []
    for b in bars5:
        k = cd.bucket_start(b.start, 15)
        if k not in groups:
            order.append(k)
        groups[k].append(b)
    for k in order:
        g = groups[k]
        need = 3 if k.hour * 60 + k.minute + 15 <= cd.SESSION_END_MIN else 0
        if need and len(g) < need:
            continue
        out.append(Bar(k, 15, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                       None if any(x.volume is None for x in g) else sum(x.volume for x in g)))
    return out


def option_model(index: str, spot: float, vix: Optional[float], points: float, hold_min: float,
                 costs: CostParams, spread_pct: float = 0.5) -> dict:
    """MODELLED option-equivalent result for one lot (see module docstring). Not a backtest of option prices."""
    lot = LOTS.get(index, 1)
    iv = (vix or 15.0) / 100 * 0.95
    dte = DTE_ASSUMED.get(index, 10)
    T = dte / 252
    premium = 0.4 * spot * iv * math.sqrt(T)              # ATM approximation
    theta_per_min = premium / (2 * dte * 375)              # ~ d(premium)/dt for ATM, per trading minute
    move = 0.5 * points - theta_per_min * hold_min
    spread = premium * spread_pct / 100
    cp = costs.for_exchange(EXCH.get(index, "NFO"))
    entry_px = premium + spread / 2
    exit_px = max(premium + move - spread / 2, 0.05)
    charges = trade_cost([("BUY", entry_px, lot), ("SELL", exit_px, lot)], cp)
    return {"premium_est": round(premium, 2), "gross_rs": round((exit_px - entry_px) * lot, 2),
            "charges_rs": round(charges, 2), "net_rs": round((exit_px - entry_px) * lot - charges, 2)}


@dataclasses.dataclass
class SimState:
    pos: Optional[Position] = None
    entries: int = 0
    losses_in_row: int = 0
    cooldown_until: Optional[dt.datetime] = None
    meta: dict = dataclasses.field(default_factory=dict)


def run(index: str, bars5: list[Bar], s: Settings, vix: dict, liquidity_pts: float = 7.0,
        costs: CostParams = CostParams()) -> tuple[list[dict], list[dict]]:
    p = s.strategy
    ens = Ensemble(p)
    exits = AdaptiveExitEngine(p.exits, p.regime)
    sess = s.session
    by_day: dict[dt.date, list[Bar]] = defaultdict(list)
    for b in bars5:
        by_day[b.start.date()].append(b)
    days = sorted(by_day)
    trades, signals = [], []
    dummy = ContractMeta(index, "X", 1, f"{index}-RESEARCH", dt.date(2100, 1, 1), 0.0, "CE", 1, 0.05)
    for n, d in enumerate(days):
        if n < 5 or len(by_day[d]) < 60:
            continue
        window = [b for k in days[n - 5:n + 1] for b in by_day[k]]
        f = Frame.build(index, window, agg15_from5(window), p.indicators)
        i0 = len(window) - len(by_day[d])
        st = SimState()
        consumed: set[str] = set()
        for i in range(i0, len(window)):
            b = window[i]
            now = b.end
            if st.pos is not None:
                pos = st.pos
                mv = (b.close - pos.entry_spot) * (1 if pos.direction == "CE" else -1)
                pos.peak_spot_move = max(pos.peak_spot_move, mv)
                st.meta["mae"] = min(st.meta["mae"], (b.low - pos.entry_spot if pos.direction == "CE"
                                                      else pos.entry_spot - b.high))
                st.meta["mfe"] = max(st.meta["mfe"], (b.high - pos.entry_spot if pos.direction == "CE"
                                                      else pos.entry_spot - b.low))
                lvl = update_structure(pos, f, i, int(p.exits.structure_trail_bars))
                if lvl is not None:
                    pos.invalidation = lvl
                a = exits.assess(pos, f, i, OptionTracker(), b.close, now, True)
                reason = None
                if now.time() >= sess.square_off:
                    reason = "SQUARE_OFF"
                elif a.action in (EXIT, REDUCE):
                    reason = "STRUCTURE" if a.structural else f"SCORE_{a.total:.0f}"
                if reason:
                    pts = (b.close - pos.entry_spot) * (1 if pos.direction == "CE" else -1)
                    hold = (now - pos.entry_time).total_seconds() / 60
                    m = st.meta
                    trades.append({"index": index, "date": d.isoformat(), "entry": pos.entry_time.strftime("%H:%M"),
                                   "exit": now.strftime("%H:%M"), "strategy": pos.strategy,
                                   "direction": pos.direction, "regime": m["regime"], "score": pos.score,
                                   "points": round(pts, 2), "pct": round(pts / pos.entry_spot * 100, 4),
                                   "mfe": round(m["mfe"], 2), "mae": round(m["mae"], 2), "hold_min": round(hold),
                                   "reason": reason, "vix": vix.get(d.isoformat()),
                                   "model": option_model(index, pos.entry_spot, vix.get(d.isoformat()), pts, hold,
                                                         costs)})
                    st.pos = None
                    st.cooldown_until = now + dt.timedelta(minutes=s.risk.cooldown_after_exit_min)
                    st.losses_in_row = st.losses_in_row + 1 if pts < 0 else 0
                continue
            if not (sess.entry_start < now.time() <= sess.entry_end):
                continue
            if st.entries >= s.risk.max_entries_per_day or st.losses_in_row >= s.risk.max_consecutive_losses:
                continue
            if st.cooldown_until and now < st.cooldown_until:
                continue
            rs = detect(f, i, p.regime)
            res = ens.evaluate(f, i, rs, consumed)
            consumed |= {c.setup_key for c, _w in res.rejected}
            for sig in res.signals:
                consumed.add(sig.candidate.setup_key)
                fwd = {}
                for h in (3, 6, 12):
                    if i + h < len(window) and window[i + h].start.date() == d:
                        fwd[f"{h * 5}m"] = round((window[i + h].close - b.close) * (1 if sig.direction == "CE" else -1),
                                                 2)
                signals.append({"date": d.isoformat(), "time": now.strftime("%H:%M"), "strategy": sig.candidate.strategy,
                                "direction": sig.direction, "regime": sig.regime, "score": sig.score,
                                "decision": sig.decision, **fwd})
            best = res.best
            if best is None:
                continue
            decision, total = ens.final_decision(best, liquidity_pts)
            if decision != ELIGIBLE or i + 1 >= len(window) or window[i + 1].start.date() != d:
                continue
            entry_px = window[i + 1].open                   # filled after the signal bar closed
            c = best.candidate
            st.pos = Position(f"{d}-{i}", index, dummy, 1, 1, 0.0, now, c.strategy, c.direction, c.setup_key, total,
                              c.invalidation, entry_px, 0.5, 0.0)
            st.entries += 1
            st.meta = {"regime": best.regime, "mfe": 0.0, "mae": 0.0}
    return trades, signals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--vix", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--liquidity-pts", type=float, default=7.0)
    ap.add_argument("--no-volume-filter", action="store_true",
                    help="history has no volume: give every bar volume 1 and drop the volume thresholds")
    ap.add_argument("--since", default=None)
    a = ap.parse_args(argv)
    s = load_settings(env={})
    if a.no_volume_filter:
        st = s.strategy
        st.orb.volume_mult = 0.0
        st.bollinger_squeeze.volume_mult = 0.0
    bars = load_bars(a.csv, 1.0 if a.no_volume_filter else None)
    if a.since:
        bars = [b for b in bars if b.start.date().isoformat() >= a.since]
    trades, signals = run(a.index, bars, s, load_vix(a.vix), a.liquidity_pts)
    out = {"index": a.index, "label": "UNDERLYING-SIGNAL RESEARCH ONLY (not an options backtest)",
           "volume_filter": not a.no_volume_filter, "liquidity_pts_assumed": a.liquidity_pts,
           "summary": summarize(trades, signals), "trades": trades, "signals_n": len(signals)}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    sm = out["summary"]
    print(f"{a.index}: {sm['all']['n']} trades, expectancy {sm['all']['exp_pts']} pts, win {sm['all']['win_rate']}%, "
          f"PF(pts) {sm['all']['pf']}, model net/lot {sm['all']['model_net_rs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
