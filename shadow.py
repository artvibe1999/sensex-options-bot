"""Shadow book: forward-tests EVERY strategy variant in parallel, in simulation.

Each variant gets at most one virtual one-lot trade per day, entered the first
time its conditions are met and exited by the same rules the bot trades with: the exit engine (structure-scaled
stop, laddered trailing floor) when it is enabled, else the variant's own stop / target / trailing rule, and always
at the square-off time. Plans use the same daily trend as the live bot and the backtest. Results go to the ``shadow_trades`` table and are
what ``promote.py`` uses to decide which variants may trade live. This runs in
both --paper and --live modes and never places orders.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

from config import IST, RiskParams, Schedule
from costs import CostParams, trade_cost
from execution import validate_plan_quotes
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
    exit_px: float = 0.0            # executable price to close right now (bid for longs, ask for shorts)


@dataclass
class _ShadowPos:
    spec: StrategySpec
    plan: TradePlan
    entry_ts: str
    legs: list[_ShadowLeg]
    max_loss: float
    max_profit: float
    peak: float = 0.0
    xpos: Optional[SimpleNamespace] = None    # the view of this position the exit engine reads and updates


def _round(p: float, tick: float) -> float:
    return round(round(p / tick) * tick, 2)


class ShadowBook:
    def __init__(self, specs: list[StrategySpec], recorder: Recorder, risk: RiskParams,
                 schedule: Schedule, costs: CostParams, slippage_pct: float = 0.005, exit_engine=None) -> None:
        self.specs, self.rec, self.risk, self.sched = specs, recorder, risk, schedule
        self.costs, self.slip = costs, slippage_pct
        self.xe = exit_engine                 # None: the variant's own percentage rules (backtest-style)
        self._sig = None
        self.open: dict[str, _ShadowPos] = {}
        self.done_today: set[str] = set()
        try:                        # a restart must not let a variant trade the same day twice
            self.done_today = set(recorder.entered_on(dt.datetime.now(IST).date().isoformat()))
        except Exception:  # noqa: BLE001 - e.g. a recorder stub without the method
            log.warning("Could not restore today's shadow entries", exc_info=True)

    def _entry_px(self, leg: Leg, chain) -> float:
        """Executable entry price: the ask for a BUY, the bid for a SELL (LTP +/- slippage if depth is missing)."""
        q = chain.get(leg.contract.strike, leg.contract.kind)
        if q is not None and q.bid and q.ask and q.ask >= q.bid > 0:
            px = q.ask if leg.side == "BUY" else q.bid
        else:
            px = leg.ref_price * (1 + self.slip if leg.side == "BUY" else 1 - self.slip)
        return _round(px, leg.contract.tick_size)

    def flatten_if_due(self, now: dt.datetime) -> None:
        """Called when no fresh chain exists (stalled feed): positions are still closed at square-off time, on their
        last marks, instead of lingering until the next day's rollover."""
        if self.open and now.time() >= self.sched.square_off:
            ts = now.isoformat(timespec="seconds")
            for vid in list(self.open):
                self._close(vid, "SQUARE_OFF_STALE", ts)

    def reset_day(self) -> None:
        for vid in list(self.open):
            self._close(vid, "DAY_ROLLOVER", dt.datetime.now(IST).isoformat(timespec="seconds"))
        self.done_today.clear()

    # ---------------------------------------------------------------- stepping
    def step(self, snap: Snapshot, now: dt.datetime, sig=None) -> None:
        """sig: market-context signals (the exit engine adapts its trail to them, as for the live position)."""
        if snap.chain is None or not snap.fresh:
            return
        self._sig = sig
        ts = now.isoformat(timespec="seconds")
        for vid in list(self.open):
            self._manage(vid, snap, now, ts)
        t = now.time()
        if not (self.sched.entry_start <= t <= self.sched.entry_end):
            return
        for spec in self.specs:
            if spec.id in self.open or spec.id in self.done_today:
                continue
            # the DAILY trend, exactly as the live bot and the backtest build plans (not the 5-minute trend_dir)
            plan = build_plan(spec, snap.chain, snap.rs, snap.trend_daily, now.date(), now, snap.features, snap.tinfo,
                              getattr(snap, "ictx", None))
            if plan is None:
                continue
            if not validate_plan_quotes(plan, snap.chain).ok:
                continue            # the live bot would refuse an unquoted / illiquid structure: so does the shadow
            legs = []
            for leg in plan.legs:
                legs.append(_ShadowLeg(leg, leg.side, self._entry_px(leg, snap.chain), leg.ref_price, leg.ref_price))
            pos = _ShadowPos(spec, plan, ts, legs, plan.max_loss_per_lot, plan.max_profit_per_lot)
            if self.xe is not None:
                pos.xpos = SimpleNamespace(
                    plan=plan, max_loss=pos.max_loss, max_profit=pos.max_profit, peak_pnl=0.0, exit_state={},
                    legs=[SimpleNamespace(contract=l.contract, side=l.side, qty=plan.qty_for(l)) for l in plan.legs])
                self.xe.init_state(pos.xpos, snap.chain, snap.spot)
            self.open[spec.id] = pos
            self.done_today.add(spec.id)
            self.rec.event(ts, "shadow_entry", spec.id, {})

    def _mark(self, pos: _ShadowPos, snap: Snapshot) -> float:
        total = 0.0
        for sl in pos.legs:
            q = snap.chain.get(sl.leg.contract.strike, sl.leg.contract.kind) \
                if snap.chain and snap.chain.expiry == pos.plan.expiry else None
            if q is not None:
                sl.last = q.ltp
                if q.bid and q.ask and q.ask >= q.bid > 0:
                    sl.exit_px = q.bid if sl.side == "BUY" else q.ask
                else:
                    sl.exit_px = q.ltp * (1 - self.slip if sl.side == "BUY" else 1 + self.slip)
            sign = 1 if sl.side == "BUY" else -1
            total += sign * (sl.last - sl.entry) * pos.plan.lot_size * sl.leg.ratio
        return total

    def _manage(self, vid: str, snap: Snapshot, now: dt.datetime, ts: str) -> None:
        pos = self.open[vid]
        pnl = self._mark(pos, snap)
        pos.peak = max(pos.peak, pnl)
        r = self.risk
        reason: Optional[str] = None
        if pos.xpos is not None:
            reason = self.xe.evaluate(pos.xpos, pnl, now, self._sig, snap.spot).reason
        elif pnl <= -pos.spec.sl_frac * pos.max_loss:
            reason = "HARD_STOP"
        elif pnl >= pos.spec.tp_frac * pos.max_profit:
            reason = "TAKE_PROFIT"
        elif (pos.peak >= r.trail_activation_pct_of_max_profit * pos.max_profit
              and pnl <= pos.peak * (1 - r.trail_giveback_pct)):
            reason = "TRAILING_STOP"
        if not reason and now.time() >= self.sched.square_off:
            reason = "SQUARE_OFF_TIME"
        if reason:
            self._close(vid, reason, ts)

    def _close(self, vid: str, reason: str, ts: str) -> None:
        pos = self.open.pop(vid)
        gross = 0.0
        fills: list[tuple[str, float, int]] = []
        for sl in pos.legs:
            qty = pos.plan.lot_size * sl.leg.ratio
            exit_side = "SELL" if sl.side == "BUY" else "BUY"
            raw = sl.exit_px or sl.last * (1 - self.slip if exit_side == "SELL" else 1 + self.slip)
            px = _round(raw, sl.leg.contract.tick_size)
            sign = 1 if sl.side == "BUY" else -1
            gross += sign * (px - sl.entry) * qty
            fills += [(sl.side, sl.entry, qty), (exit_side, px, qty)]
        cost = trade_cost(fills, self.costs)
        self.rec.shadow_trade(vid, pos.entry_ts, ts, reason, pos.plan.regime,
                              gross, cost, gross - cost, pos.max_loss)
