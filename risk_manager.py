"""Position sizing, stop-losses, trailing stops and daily loss limits."""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import RiskParams, Schedule, risk_for_capital
from strategy_engine import TradePlan
from zerodha_client import OptionContract

log = logging.getLogger(__name__)


@dataclass
class OpenLeg:
    contract: OptionContract
    side: str
    qty: int
    entry_price: float


@dataclass
class ManagedPosition:
    plan: TradePlan
    lots: int
    legs: list[OpenLeg]
    entered_at: dt.datetime
    max_loss: float
    max_profit: float
    peak_pnl: float = 0.0

    def pnl(self, ltps: dict[int, float]) -> float:
        total = 0.0
        for leg in self.legs:
            sign = 1 if leg.side == "BUY" else -1
            total += sign * (ltps[leg.contract.token] - leg.entry_price) * leg.qty
        return total


class RiskManager:
    def __init__(self, params: RiskParams, schedule: Schedule) -> None:
        self.p, self._base, self.sched = params, params, schedule
        self.halted = False
        self.halt_reason = ""
        self._day_peak = 0.0

    def apply_capital(self, capital: float, profile: str = "auto") -> None:
        """Pick the standard or small-account risk limits for this account size (call once capital is known)."""
        self.p = risk_for_capital(self._base, capital, profile)
        log.info("Risk profile for capital %.0f: per-trade %.1f%%, one-lot cap %.0f%%, daily loss %.0f%%",
                 capital, self.p.max_risk_per_trade_pct * 100, self.p.one_lot_risk_pct * 100,
                 self.p.max_daily_loss_pct * 100)

    # -------------------------------------------------------------- sizing
    def size(self, plan: TradePlan, capital: float,
             margin_fn: Optional[Callable[[int], Optional[float]]] = None) -> int:
        """Lots such that worst-case loss <= risk budget and margin <= cap.

        If a margin function is supplied and fails, size fails closed (0 lots).
        """
        if plan.max_loss_per_lot <= 0:
            return 0
        budget = capital * self.p.max_risk_per_trade_pct
        lots = min(self.p.max_lots, math.floor(budget / plan.max_loss_per_lot))
        if lots < 1 and plan.max_loss_per_lot <= capital * self.p.one_lot_risk_pct:
            lots = 1                                   # small account: one lot, capped by one_lot_risk_pct
        if lots < 1:
            log.debug("Risk budget %.0f < max loss per lot %.0f", budget, plan.max_loss_per_lot)
            return 0
        if margin_fn is not None:
            while lots >= 1:
                m = margin_fn(lots)
                if m is None:
                    return 0
                if m <= capital * self.p.margin_utilisation:
                    break
                lots -= 1
        return max(lots, 0)

    # -------------------------------------------------------------- gating
    def entry_allowed(self, now: dt.datetime, trades_today: int) -> tuple[bool, str]:
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        if trades_today >= self.p.max_trades_per_day:
            return False, "max trades reached"
        t = now.time()
        if not (self.sched.entry_start <= t <= self.sched.entry_end):
            return False, "outside entry window"
        return True, ""

    # --------------------------------------------------------------- exits
    def update_portfolio(self, capital: float, day_pnl: float) -> Optional[str]:
        """Daily loss limit and portfolio trailing stop. Returns a reason to flatten."""
        if self.halted:
            return self.halt_reason
        if day_pnl <= -capital * self.p.max_daily_loss_pct:
            return self._halt("DAILY_LOSS_LIMIT")
        self._day_peak = max(self._day_peak, day_pnl)
        if self._day_peak >= capital * self.p.portfolio_trail_activation_pct:
            floor = self._day_peak * (1 - self.p.portfolio_trail_giveback_pct)
            if day_pnl <= floor:
                return self._halt("PORTFOLIO_TRAILING_STOP")
        return None

    def check_position(self, pos: ManagedPosition, pnl: float) -> Optional[str]:
        p = self.p
        sl = pos.plan.sl_frac if pos.plan.sl_frac is not None else p.stop_loss_pct_of_max_loss
        tp = pos.plan.tp_frac if pos.plan.tp_frac is not None else p.take_profit_pct_of_max_profit
        pos.peak_pnl = max(pos.peak_pnl, pnl)
        if pnl <= -sl * pos.max_loss:
            return "HARD_STOP"
        if pnl >= tp * pos.max_profit:
            return "TAKE_PROFIT"
        if (pos.peak_pnl >= p.trail_activation_pct_of_max_profit * pos.max_profit
                and pnl <= pos.peak_pnl * (1 - p.trail_giveback_pct)):
            return "TRAILING_STOP"
        return None

    def _halt(self, reason: str) -> str:
        self.halted, self.halt_reason = True, reason
        log.critical("RISK HALT: %s", reason)
        return reason

    def halt(self, reason: str) -> str:
        """Public halt (e.g. position reconciliation mismatch)."""
        return self._halt(reason)

    def reset_day(self) -> None:
        self.halted, self.halt_reason, self._day_peak = False, "", 0.0

    def export(self) -> dict:
        return {"halted": self.halted, "reason": self.halt_reason, "day_peak": self._day_peak}

    def restore(self, d: dict) -> None:
        self.halted = bool(d.get("halted", False))
        self.halt_reason = d.get("reason", "")
        self._day_peak = float(d.get("day_peak", 0.0))
