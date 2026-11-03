"""Ensemble (brief 4.8): collect every strategy's candidates for a completed bar, apply the regime permission table,
dedupe, reject CE/PE conflicts, score, rank. Produces Signals; never orders.

Conflict policy (documented): if the same index produces both a CE and a PE candidate on the same bar (from any
strategies, before regime filtering), every candidate of that bar is rejected: the evidence is mixed.
Ranking: score, then the number of distinct strategy families agreeing on the direction, then strategy order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterable, Optional

from ..features import Frame
from ..models import Candidate, Signal
from ..regime import HIGH_VOL, PERMISSIONS, RegimeState
from . import scorer
from .bollinger_squeeze import BollingerSqueeze
from .ema_pullback import EmaPullback
from .failed_breakout import FailedBreakout
from .orb import OpeningRangeBreakout
from .vwap_continuation import VwapContinuation

log = logging.getLogger(__name__)

ELIGIBLE, WATCHLIST, REJECT = "ELIGIBLE", "WATCHLIST", "REJECT"


def build_strategies(p: SimpleNamespace) -> list:
    return [OpeningRangeBreakout(p.orb), VwapContinuation(p.vwap_continuation), EmaPullback(p.ema_pullback),
            FailedBreakout(p.failed_breakout), BollingerSqueeze(p.bollinger_squeeze)]


def permitted(c: Candidate, regime: str) -> tuple[bool, str]:
    rule = PERMISSIONS[regime][c.strategy]
    if rule == "block":
        return False, f"{c.strategy} blocked in {regime}"
    if rule in ("CE", "PE"):
        return (c.direction == rule, f"{regime} allows {rule} only")
    if rule in ("CE_strong", "PE_strong"):
        side = rule[:2]
        ok = c.direction == side and c.strong
        return ok, f"{regime} allows only a strong {side} failed breakout"
    if rule == "confirmed":
        return c.strong, "RANGE: only a confirmed (volume) range expansion"
    return True, ""                                     # both / selective


@dataclass
class BarResult:
    signals: list                                       # every scored Signal (for the journal)
    best: Optional[Signal]
    rejected: list                                      # (candidate, reason) never scored


class Ensemble:
    def __init__(self, p: SimpleNamespace, strategies: Optional[list] = None) -> None:
        self.p = p
        self.strategies = strategies if strategies is not None else build_strategies(p)
        self._order = {s.id: n for n, s in enumerate(self.strategies)}

    def candidates(self, f: Frame, i: int) -> list[Candidate]:
        out: list[Candidate] = []
        for s in self.strategies:
            try:
                out += s.evaluate(f, i)
            except Exception:  # noqa: BLE001 - one broken strategy must not stop the others
                log.exception("Strategy %s failed on %s", s.id, f.index)
        return out

    def evaluate(self, f: Frame, i: int, rs: RegimeState, consumed: Iterable[str] = ()) -> BarResult:
        consumed = set(consumed)
        cands = [c for c in self.candidates(f, i) if c.setup_key not in consumed]
        # dedupe identical setups from one strategy
        seen, uniq = set(), []
        for c in cands:
            k = (c.strategy, c.setup_key)
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        rejected: list[tuple[Candidate, str]] = []
        if len({c.direction for c in uniq}) > 1:
            return BarResult([], None, [(c, "CE/PE conflict on the same bar") for c in uniq])
        sc = self.p.scoring
        signals: list[Signal] = []
        for c in uniq:
            ok, why = permitted(c, rs.regime)
            if not ok:
                rejected.append((c, why))
                continue
            total, comp, reasons = scorer.score(c, f, i, self.p.regime)
            need = sc.eligible + (sc.high_vol_extra if rs.regime == HIGH_VOL else 0)
            # pre-liquidity decision: the option-liquidity points are added after contract selection
            decision = ELIGIBLE if total + 10 >= need else (WATCHLIST if total + 10 >= sc.watchlist else REJECT)
            signals.append(Signal(c, rs.regime, total, comp, decision, tuple(c.reasons) + tuple(reasons)))
        agree = {}
        for s in signals:
            agree.setdefault(s.direction, set()).add(s.candidate.strategy)
        elig = [s for s in signals if s.decision == ELIGIBLE]
        elig.sort(key=lambda s: (-s.score, -len(agree.get(s.direction, ())), self._order.get(s.candidate.strategy, 9)))
        return BarResult(signals, elig[0] if elig else None, rejected)

    def final_decision(self, sig: Signal, liquidity_pts: float) -> tuple[str, float]:
        """Decision once the selected contract's liquidity points are known."""
        sc = self.p.scoring
        total = round(sig.score + liquidity_pts, 1)
        need = sc.eligible + (sc.high_vol_extra if sig.regime == HIGH_VOL else 0)
        if total >= need:
            return ELIGIBLE, total
        return (WATCHLIST if total >= sc.watchlist else REJECT), total
