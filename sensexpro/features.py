"""All indicator series for one index, computed once per completed 5-minute candle (Frame).

Strategies, the regime detector, the scorer and the exit engine read the same Frame, so they always agree on the
numbers. Index i of every series refers to bars[i]; 15-minute context at i is the last 15-minute bar that had closed by
bars[i].end (never a forming one).
"""
from __future__ import annotations

import bisect
import datetime as dt
import statistics
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional, Sequence

from . import indicators as ind
from .models import Bar


@dataclass
class Frame:
    index: str
    bars: list[Bar]                      # completed 5-minute bars, several sessions
    bars15: list[Bar]                    # completed 15-minute bars
    p: SimpleNamespace                   # strategy.indicators
    ema_f: list = field(default_factory=list)
    ema_s: list = field(default_factory=list)
    rsi: list = field(default_factory=list)
    macd_hist: list = field(default_factory=list)
    atr: list = field(default_factory=list)
    vwap: list = field(default_factory=list)
    vwap_fallback: bool = False
    bb_mid: list = field(default_factory=list)
    bb_up: list = field(default_factory=list)
    bb_lo: list = field(default_factory=list)
    bb_bw: list = field(default_factory=list)
    vol_ratio: list = field(default_factory=list)
    ema15_f: list = field(default_factory=list)
    ema15_s: list = field(default_factory=list)
    adx15: list = field(default_factory=list)
    _ends15: list = field(default_factory=list)

    @classmethod
    def build(cls, index: str, bars: Sequence[Bar], bars15: Sequence[Bar], p: SimpleNamespace) -> "Frame":
        f = cls(index, list(bars), list(bars15), p)
        c = [b.close for b in f.bars]
        f.ema_f, f.ema_s = ind.ema(c, p.ema_fast), ind.ema(c, p.ema_slow)
        f.rsi = ind.rsi(c, p.rsi)
        _l, _s, f.macd_hist = ind.macd(c, *p.macd)
        f.atr = ind.atr(f.bars, p.atr)
        f.vwap, f.vwap_fallback = ind.session_vwap(f.bars)
        f.bb_mid, f.bb_up, f.bb_lo, f.bb_bw = ind.bollinger(c, p.bb_period, p.bb_std)
        f.vol_ratio = ind.volume_ratio(f.bars, p.volume_baseline)
        c15 = [b.close for b in f.bars15]
        f.ema15_f, f.ema15_s = ind.ema(c15, p.ema_fast), ind.ema(c15, p.ema_slow)
        f.adx15, _p, _m = ind.adx(f.bars15, p.adx)
        f._ends15 = [b.end for b in f.bars15]
        return f

    # ------------------------------------------------------------------ navigation
    @property
    def last(self) -> int:
        return len(self.bars) - 1

    def day_start(self, i: int) -> int:
        d = self.bars[i].start.date()
        j = i
        while j > 0 and self.bars[j - 1].start.date() == d:
            j -= 1
        return j

    def j15(self, i: int) -> Optional[int]:
        """Index of the last 15-minute bar closed by bars[i].end."""
        k = bisect.bisect_right(self._ends15, self.bars[i].end) - 1
        return k if k >= 0 else None

    # ------------------------------------------------------------------ context helpers
    def trend15(self, i: int) -> int:
        """+1 bullish / -1 bearish / 0 mixed 15-minute context (EMA structure, EMA slope, close vs slow EMA)."""
        j = self.j15(i)
        if j is None or j < 1:
            return 0
        f, s, s0 = self.ema15_f[j], self.ema15_s[j], self.ema15_s[j - 1]
        if f is None or s is None or s0 is None:
            return 0
        c = self.bars15[j].close
        if f > s and s >= s0 and c > s:
            return 1
        if f < s and s <= s0 and c < s:
            return -1
        return 0

    def strong_against(self, i: int, direction: str, strong_adx: float = 25.0) -> bool:
        """15-minute context strongly opposed to `direction` (trend against it and ADX above strong_adx)."""
        t = self.trend15(i)
        j = self.j15(i)
        a = self.adx15[j] if j is not None else None
        against = t == -1 if direction == "CE" else t == 1
        return against and a is not None and a >= strong_adx

    def vwap_slope(self, i: int, lookback: Optional[int] = None) -> Optional[float]:
        lb = lookback or self.p.vwap_slope_bars
        if i - lb < self.day_start(i):
            return None
        return ind.slope(self.vwap, i, lb)

    def vwap_crosses(self, i: int) -> int:
        """Number of times the close switched sides of VWAP in this session up to bar i."""
        n, prev = 0, None
        for k in range(self.day_start(i), i + 1):
            v = self.vwap[k]
            if v is None:
                continue
            side = self.bars[k].close > v
            if prev is not None and side != prev:
                n += 1
            prev = side
        return n

    def opening_range(self, i: int, minutes: int = 15) -> Optional[tuple[float, float, dt.datetime]]:
        """(high, low, end time) of the session's first `minutes`, if complete by bar i."""
        d0 = self.day_start(i)
        first = self.bars[d0]
        if first.start.hour * 60 + first.start.minute != 9 * 60 + 15:
            return None                                    # session's first candle missing: no reliable range
        end = first.start + dt.timedelta(minutes=minutes)
        rng = [b for b in self.bars[d0:i + 1] if b.end <= end]
        if not rng or rng[-1].end != end:
            return None
        return max(b.high for b in rng), min(b.low for b in rng), end

    def prev_session_hl(self, i: int) -> Optional[tuple[float, float]]:
        d0 = self.day_start(i)
        if d0 == 0:
            return None
        p0 = self.day_start(d0 - 1)
        seg = self.bars[p0:d0]
        return max(b.high for b in seg), min(b.low for b in seg)

    def atr_ratio(self, i: int, sessions: int = 20) -> Optional[float]:
        """ATR now / median ATR at the same bars of earlier sessions available (no future data)."""
        a = self.atr[i]
        if a is None:
            return None
        prior = [x for x in self.atr[:self.day_start(i)] if x is not None]
        prior = prior[-sessions * 75:]
        if len(prior) < 75:
            return None
        med = statistics.median(prior)
        return a / med if med > 0 else None

    def has_gap(self, i: int, lookback: int = 6) -> bool:
        return any(b.gap for b in self.bars[max(0, i - lookback + 1):i + 1])
