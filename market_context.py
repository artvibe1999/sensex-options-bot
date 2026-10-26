"""Rolling market context shared by the exit engine and the entry filter.

`MarketContext.update(snapshot)` is called once per fresh market snapshot. It keeps a short history of spot, India
VIX, PCR, max pain and total open interest, and `signals()` turns that history into a small set of numbers:

  * spot momentum over the window and the retrace from the intraday extreme (reversal / exhaustion inputs)
  * PCR momentum and max-pain drift (positioning shifts)
  * VIX change (volatility shifts)
  * OI build-up classification (long / short build-up, short covering, long unwinding)

Pure Python, no I/O; every input may be missing and the result then simply carries None / NEUTRAL, so a data gap
can never raise inside the trading loop.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

LONG_BUILDUP, SHORT_BUILDUP = "LONG_BUILDUP", "SHORT_BUILDUP"
SHORT_COVERING, LONG_UNWINDING = "SHORT_COVERING", "LONG_UNWINDING"
NEUTRAL = "NEUTRAL"
BUILDUP_BIAS = {LONG_BUILDUP: 1, SHORT_COVERING: 1, SHORT_BUILDUP: -1, LONG_UNWINDING: -1, NEUTRAL: 0}


def classify_oi_buildup(spot_chg_pct: Optional[float], call_oi_chg: Optional[float], put_oi_chg: Optional[float],
                        total_oi: Optional[float], min_spot_pct: float = 0.05, min_oi_pct: float = 0.1) -> str:
    """Classic price-vs-OI reading applied to the whole option chain.

    Net OI change = call + put OI change (contracts) over the same window as the spot change.
      price up   & OI up   -> LONG_BUILDUP     price down & OI up   -> SHORT_BUILDUP
      price up   & OI down -> SHORT_COVERING   price down & OI down -> LONG_UNWINDING
    Small moves (under `min_spot_pct` of spot or `min_oi_pct` of total OI) are NEUTRAL: noise is not a signal.
    """
    if spot_chg_pct is None or call_oi_chg is None or put_oi_chg is None or not total_oi or total_oi <= 0:
        return NEUTRAL
    oi_pct = 100.0 * (call_oi_chg + put_oi_chg) / total_oi
    if abs(spot_chg_pct) < min_spot_pct or abs(oi_pct) < min_oi_pct:
        return NEUTRAL
    if spot_chg_pct > 0:
        return LONG_BUILDUP if oi_pct > 0 else SHORT_COVERING
    return SHORT_BUILDUP if oi_pct > 0 else LONG_UNWINDING


def writing_bias(spot_chg_pct: Optional[float], call_oi_chg: Optional[float], put_oi_chg: Optional[float],
                 ratio: float = 1.25) -> int:
    """Institutional option WRITING read: +1 when puts are being written faster than calls (support building),
    -1 when calls are written faster than puts (resistance building), 0 otherwise or when OI is not building."""
    if call_oi_chg is None or put_oi_chg is None:
        return 0
    if put_oi_chg > 0 and put_oi_chg >= ratio * max(call_oi_chg, 0.0) and (spot_chg_pct is None or spot_chg_pct >= 0):
        return 1
    if call_oi_chg > 0 and call_oi_chg >= ratio * max(put_oi_chg, 0.0) and (spot_chg_pct is None or spot_chg_pct <= 0):
        return -1
    return 0


@dataclass(frozen=True)
class ContextSignals:
    ts: dt.datetime
    spot: float
    spot_chg_pct: Optional[float] = None      # over the window (default ~5 min)
    spot_chg_short_pct: Optional[float] = None  # over the short window (~1-2 min): momentum turning
    pcr: Optional[float] = None
    pcr_mom: Optional[float] = None           # PCR now - PCR a window ago
    max_pain: Optional[float] = None
    max_pain_drift: Optional[float] = None    # index points over the window
    vix: Optional[float] = None
    vix_chg_pct: Optional[float] = None       # percent change over the window
    call_oi_chg: Optional[float] = None
    put_oi_chg: Optional[float] = None
    total_oi: Optional[float] = None
    buildup: str = NEUTRAL
    write_bias: int = 0
    day_high: Optional[float] = None
    day_low: Optional[float] = None

    @property
    def buildup_bias(self) -> int:
        return BUILDUP_BIAS.get(self.buildup, 0)


def _chg_pct(now: float, then: Optional[float]) -> Optional[float]:
    if then is None or then <= 0 or now is None or math.isnan(now):
        return None
    return 100.0 * (now - then) / then


class MarketContext:
    def __init__(self, sample_every_s: float = 5.0, keep_s: float = 1800.0) -> None:
        self.sample_every_s, self.keep_s = sample_every_s, keep_s
        # (monotonic-ish epoch seconds, spot, vix, pcr, max_pain, total_oi)
        self._h: deque[tuple[float, float, float, Optional[float], Optional[float], Optional[float]]] = deque()
        self._hi: Optional[float] = None
        self._lo: Optional[float] = None
        self._last: Optional[ContextSignals] = None

    def reset(self) -> None:
        self._h.clear()
        self._hi = self._lo = None
        self._last = None

    # ---------------------------------------------------------------- input
    def update(self, snap, now: dt.datetime) -> None:
        """Record one fresh snapshot. Never raises: a malformed snapshot is logged and skipped."""
        try:
            t = now.timestamp()
            spot = float(snap.spot)
            if spot <= 0 or math.isnan(spot):
                return
            self._hi = spot if self._hi is None else max(self._hi, spot)
            self._lo = spot if self._lo is None else min(self._lo, spot)
            if self._h and t - self._h[-1][0] < self.sample_every_s:
                return
            f = getattr(snap, "features", None)
            total_oi = None
            if getattr(snap, "chain", None) is not None:
                total_oi = float(sum(q.oi for q in snap.chain.quotes.values() if q.oi and q.oi > 0)) or None
            pcr = None if f is None or f.pcr is None or math.isnan(f.pcr) else f.pcr
            self._h.append((t, spot, float(snap.vix or 0.0), pcr, getattr(f, "max_pain", None), total_oi))
            while self._h and t - self._h[0][0] > self.keep_s:
                self._h.popleft()
            self._last = self._compute(snap, now)
        except Exception:  # noqa: BLE001 - context must never break the trading loop
            log.exception("MarketContext.update failed (ignored)")

    # --------------------------------------------------------------- output
    def _at_or_before(self, t: float) -> Optional[tuple]:
        best = None
        for row in self._h:
            if row[0] <= t:
                best = row
            else:
                break
        return best

    def spot_change_pct(self, window_s: float, now: Optional[dt.datetime] = None) -> Optional[float]:
        if not self._h:
            return None
        t_now = now.timestamp() if now else self._h[-1][0]
        ref = self._at_or_before(t_now - window_s)
        return None if ref is None else _chg_pct(self._h[-1][1], ref[1])

    def _compute(self, snap, now: dt.datetime, window_s: float = 300.0, short_s: float = 90.0) -> ContextSignals:
        t, spot, vix, pcr, mp, total_oi = self._h[-1]
        ref = self._at_or_before(t - window_s)
        ref_s = self._at_or_before(t - short_s)
        f = getattr(snap, "features", None)
        call_chg = getattr(f, "call_oi_chg", None)
        put_chg = getattr(f, "put_oi_chg", None)
        spot_chg = None if ref is None else _chg_pct(spot, ref[1])
        return ContextSignals(
            ts=now, spot=spot, spot_chg_pct=spot_chg,
            spot_chg_short_pct=None if ref_s is None else _chg_pct(spot, ref_s[1]),
            pcr=pcr, pcr_mom=None if ref is None or ref[3] is None or pcr is None else pcr - ref[3],
            max_pain=mp, max_pain_drift=None if ref is None or ref[4] is None or mp is None else mp - ref[4],
            vix=vix or None, vix_chg_pct=None if ref is None or not ref[2] or not vix else _chg_pct(vix, ref[2]),
            call_oi_chg=call_chg, put_oi_chg=put_chg, total_oi=total_oi,
            buildup=classify_oi_buildup(spot_chg, call_chg, put_chg, total_oi),
            write_bias=writing_bias(spot_chg, call_chg, put_chg),
            day_high=self._hi, day_low=self._lo)

    def signals(self) -> Optional[ContextSignals]:
        return self._last
