"""Shadow book: forward-tests EVERY strategy variant in parallel, in simulation.

Each variant gets at most one virtual one-lot trade per day, entered the first
time its conditions are met and exited by its own stop / target / trailing rule
or at the square-off time. Results go to the ``shadow_trades`` table and are
what ``promote.py`` uses to decide which variants may trade live. This runs in
both --paper and --live modes and never places orders.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import RiskParams, Schedule
from costs import CostParams, trade_cost
from market_data import Snapshot
from recorder import Recorder
from strategy_engine import Leg, TradePlan
from strategy_library import StrategySpec, build_plan

log = logging.getLogger(__name__)


@dataclass
class _ShadowLeg:
    leg: Leg
    side: str
    entry: float
    last: float


@dataclass
class _ShadowPos:
    spec: StrategySpec
    plan: TradePlan
    entry_ts: str
    legs: list[_ShadowLeg]
    max_loss: float
    max_profit: float
    peak: float = 0.0


def _round(p: float, tick: float) -> float:
    return round(round(p / tick) * tick, 2)


class ShadowBook:
    def __init__(self, specs: list[StrategySpec], recorder: Recorder, risk: RiskParams,
                 schedule: Schedule, costs: CostParams, slippage_pct: float = 0.005) -> None:
        self.specs, self.rec, self.risk, self.sched = specs, recorder, risk, schedule
        self.costs, self.slip = costs, slippage_pct
        self.open: dict[str, _ShadowPos] = {}
        self.done_today: set[str] = set()

    def reset_day(self) -> None:
        for vid in list(self.open):
            self._close(vid, "DAY_ROLLOVER", dt.datetime.now().isoformat(timespec="seconds"))
        self.done_today.clear()

    # ---------------------------------------------------------------- stepping
    def step(self, snap: Snapshot, now: dt.datetime) -> None:
        if snap.chain is None or not snap.fresh:
            return
        ts = now.isoformat(timespec="seconds")
        for vid in list(self.open):
            self._manage(vid, snap, now, ts)
        t = now.time()
        if not (self.sched.entry_start <= t <= self.sched.entry_end):
            return
        for spec in self.specs:
            if spec.id in self.open or spec.id in self.done_today:
                continue
            plan = build_plan(spec, snap.chain, snap.rs, snap.trend_dir, now.date(), now)
            if plan is None:
                continue
            legs = []
            for leg in plan.legs:
                px = leg.ref_price * (1 + self.slip if leg.side == "BUY" else 1 - self.slip)
                legs.append(_ShadowLeg(leg, leg.side, _round(px, leg.contract.tick_size), leg.ref_price))
            self.open[spec.id] = _ShadowPos(spec, plan, ts, legs, plan.max_loss_per_lot,
                                            plan.max_profit_per_lot)
            self.done_today.add(spec.id)

    def _mark(self, pos: _ShadowPos, snap: Snapshot) -> float:
        total = 0.0
        for sl in pos.legs:
            q = snap.chain.get(sl.leg.contract.strike, sl.leg.contract.kind) \
                if snap.chain and snap.chain.expiry == pos.plan.expiry else None
            if q is not None:
                sl.last = q.ltp
            sign = 1 if sl.side == "BUY" else -1
            total += sign * (sl.last - sl.entry) * pos.plan.lot_size
        return total

    def _manage(self, vid: str, snap: Snapshot, now: dt.datetime, ts: str) -> None:
        pos = self.open[vid]
        pnl = self._mark(pos, snap)
        pos.peak = max(pos.peak, pnl)
        r = self.risk
        reason: Optional[str] = None
        if pnl <= -pos.spec.sl_frac * pos.max_loss:
            reason = "HARD_STOP"
        elif pnl >= pos.spec.tp_frac * pos.max_profit:
            reason = "TAKE_PROFIT"
        elif (pos.peak >= r.trail_activation_pct_of_max_profit * pos.max_profit
              and pnl <= pos.peak * (1 - r.trail_giveback_pct)):
            reason = "TRAILING_STOP"
        elif now.time() >= self.sched.square_off:
            reason = "SQUARE_OFF_TIME"
        if reason:
            self._close(vid, reason, ts)

    def _close(self, vid: str, reason: str, ts: str) -> None:
        pos = self.open.pop(vid)
        gross = 0.0
        fills: list[tuple[str, float, int]] = []
        qty = pos.plan.lot_size
        for sl in pos.legs:
            exit_side = "SELL" if sl.side == "BUY" else "BUY"
            px = _round(sl.last * (1 - self.slip if exit_side == "SELL" else 1 + self.slip),
                        sl.leg.contract.tick_size)
            sign = 1 if sl.side == "BUY" else -1
            gross += sign * (px - sl.entry) * qty
            fills += [(sl.side, sl.entry, qty), (exit_side, px, qty)]
        cost = trade_cost(fills, self.costs)
        self.rec.shadow_trade(vid, pos.entry_ts, ts, reason, pos.plan.regime,
                              gross, cost, gross - cost, pos.max_loss)
