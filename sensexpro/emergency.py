"""Mandatory emergency capital protection (brief 7.9). Independent of the adaptive exit engine: it is evaluated first
on every loop, its decision overrides any exit score, and no strategy or config flag can switch it off (only the
thresholds are configurable, and settings.py refuses non-positive ones).

It does NOT guarantee the configured loss: gaps, illiquidity, circuit limits, rejected orders or outages can make the
loss larger, up to the whole premium.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .models import OptionQuote, Position
from .risk import RiskBook, breaker_breach
from .settings import RiskLimits, Session


@dataclass(frozen=True)
class EmergencyTrigger:
    kind: str           # trade_loss | daily_loss | weekly_loss | drawdown | broker_mismatch | unreconciled |
                        # stale_quote | wide_spread | session_close
    detail: str
    latch: bool         # latch the kill switch (manual reset required)


class EmergencyProtection:
    def __init__(self, limits: RiskLimits, session: Session) -> None:
        self.limits, self.session = limits, session
        self._wide_since: Optional[float] = None
        self._stale_since: Optional[float] = None

    def set_limits(self, limits: RiskLimits) -> None:
        self.limits = limits

    def _t(self, kind: str, detail: str) -> EmergencyTrigger:
        return EmergencyTrigger(kind, detail, kind in self.limits.latch_on)

    def check(self, pos: Position, quote: Optional[OptionQuote], unrealized: Optional[float], book: RiskBook,
              now: dt.datetime, now_mono: float, broker_ok: bool = True, reconciled: bool = True,
              quote_problem: Optional[str] = None) -> Optional[EmergencyTrigger]:
        """The first trigger that applies, else None. `unrealized` is the position's mark at the bid after charges
        (None when no executable bid exists)."""
        L = self.limits
        if not broker_ok:
            return self._t("broker_mismatch", "broker reports an unexpected position or quantity")
        if not reconciled:
            return self._t("unreconciled", "order state could not be reconciled with the broker")
        if unrealized is not None:
            if unrealized <= -L.emergency_loss_per_trade:
                return self._t("trade_loss", f"position loss {unrealized:,.0f} breached the emergency budget "
                                             f"{L.emergency_loss_per_trade:,.0f}")
            b = breaker_breach(book, L, unrealized)
            if b:
                return self._t(b.kind, b.detail)
        # stale / missing quote while holding (the caller has already tried a REST refresh)
        if quote_problem:
            self._stale_since = self._stale_since if self._stale_since is not None else now_mono
            if now_mono - self._stale_since >= L.emergency_stale_s:
                return self._t("stale_quote", f"no executable quote for {now_mono - self._stale_since:.0f}s "
                                              f"({quote_problem})")
        else:
            self._stale_since = None
        sp = quote.spread_pct if quote is not None else None
        if sp is not None and sp > L.emergency_spread_pct:
            self._wide_since = self._wide_since if self._wide_since is not None else now_mono
            if now_mono - self._wide_since >= L.emergency_spread_persist_s:
                return self._t("wide_spread", f"spread {sp:.1f}% > {L.emergency_spread_pct}% for "
                                              f"{now_mono - self._wide_since:.0f}s")
        else:
            self._wide_since = None
        if now.time() >= self.session.square_off or pos.contract.expiry < now.date():
            return EmergencyTrigger("session_close", f"square-off time {self.session.square_off:%H:%M}", False)
        return None

    def reset(self) -> None:
        self._wide_since = self._stale_since = None
