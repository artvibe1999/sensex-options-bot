"""Paper-only exploration: turn the opportunity scanner's best setup into an actual paper trade.

WHY: the normal path trades only variants that passed the backtest, and with a small account (or a model that
approves nothing) that means NO paper trades and therefore no learning. In paper mode the scanner's top setups
(valid trend, failed-breakout reversal, range, vol expansion) are instead traded with the cheapest-to-run
defined-risk variant that fits the account. These are experiments: they are labelled "[EXPLORE ...]" in the
rationale, they never run in --live mode, and nothing here proves an edge. Whether they make money after costs is
decided by the recorded results, not by this module.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable, Optional

from execution import validate_plan_quotes
from scanner import affordable, scan
from strategy_engine import TradePlan
from strategy_library import StrategySpec, _round_trip_cost, build_plan


def pick_explore(snap, specs: list[StrategySpec], capital: float, size_fn: Callable[[TradePlan], int],
                 now: dt.datetime, min_score: float = 60.0, max_setups: int = 2,
                 gate: Optional[Callable[[TradePlan, int], bool]] = None) -> Optional[tuple[TradePlan, int, float]]:
    """Best (plan, lots, scanner score) for the scanner's leading setups, or None. A candidate must be quoted and
    liquid, fit the account, and keep a positive expected margin after costs: (target profit - round-trip cost)
    relative to the stop-loss amount. The highest margin wins, which naturally favours cheap, few-leg structures
    on small accounts."""
    if snap is None or snap.chain is None or not snap.fresh:
        return None
    by_id = {s.id: s for s in specs}
    best: Optional[tuple[float, TradePlan, int, float]] = None
    for opp in scan(snap, min_score)[:max_setups]:
        for vid in affordable(opp, specs, capital):
            spec = by_id[vid]
            plan = build_plan(spec, snap.chain, snap.rs, snap.trend_daily, now.date(), now, snap.features, snap.tinfo)
            if plan is None or not validate_plan_quotes(plan, snap.chain).ok:
                continue
            lots = size_fn(plan)
            if lots < 1 or (gate is not None and not gate(plan, lots)):
                continue
            target = spec.tp_frac * plan.max_profit_per_lot
            stop = spec.sl_frac * plan.max_loss_per_lot
            margin = (target - _round_trip_cost(plan.legs)) / stop if stop > 0 else 0.0
            if margin <= 0:
                continue
            if best is None or margin > best[0]:
                best = (margin, plan, lots, opp.score)
                tagged = dataclasses.replace(plan, rationale=f"[EXPLORE {opp.setup} {opp.score:.0f}] {plan.rationale}")
                best = (margin, tagged, lots, opp.score)
    return None if best is None else (best[1], best[2], best[3])
