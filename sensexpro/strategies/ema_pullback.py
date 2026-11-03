"""Strategy 3: EMA pullback continuation (brief 4.3). Controlled retracement into the 9/20 EMA zone of a directional
market, triggered by a close beyond the prior candle's extreme."""
from __future__ import annotations

from types import SimpleNamespace

from ..features import Frame
from .base import CE, PE, Candidate, day_key, directional_strength, ready


class EmaPullback:
    id = "ema_pullback"
    PULLBACK_BARS = 3
    CROSS_LOOKBACK = 12

    def __init__(self, p: SimpleNamespace) -> None:
        self.p = p

    def evaluate(self, f: Frame, i: int) -> list[Candidate]:
        p = self.p
        if i < self.CROSS_LOOKBACK + 2 or not ready(f, i, f.ema_f, f.ema_s, f.atr, f.rsi) \
                or f.ema_f[i - 2] is None or f.ema_s[i - 2] is None:
            return []
        if i - f.day_start(i) < self.PULLBACK_BARS:
            return []
        b, prev = f.bars[i], f.bars[i - 1]
        e9, e20, atr = f.ema_f[i], f.ema_s[i], f.atr[i]
        if abs(e9 - e20) < p.min_ema_gap_atr * atr:
            return []                                    # flat EMAs
        crosses = 0
        for k in range(i - self.CROSS_LOOKBACK + 1, i + 1):
            a0, b0, a1, b1 = f.ema_f[k - 1], f.ema_s[k - 1], f.ema_f[k], f.ema_s[k]
            if None not in (a0, b0, a1, b1) and (a0 > b0) != (a1 > b1):
                crosses += 1
        if crosses > 1:
            return []                                    # EMAs repeatedly crossing
        t15 = f.trend15(i)
        out = []
        for direction in (CE, PE):
            up = direction == CE
            rising = e9 > f.ema_f[i - 2] and e20 > f.ema_s[i - 2]
            falling = e9 < f.ema_f[i - 2] and e20 < f.ema_s[i - 2]
            if up and not (e9 > e20 and rising and b.close > e20 and t15 == 1):
                continue
            if not up and not (e9 < e20 and falling and b.close < e20 and t15 == -1):
                continue
            seg = range(i - self.PULLBACK_BARS, i)
            touched = any((f.bars[k].low <= f.ema_f[k] + p.pullback_atr * atr) if up
                          else (f.bars[k].high >= f.ema_f[k] - p.pullback_atr * atr) for k in seg)
            broke = any((f.bars[k].close < f.ema_s[k]) if up else (f.bars[k].close > f.ema_s[k]) for k in seg)
            if not touched or broke:
                continue
            trigger = b.close > prev.high if up else b.close < prev.low
            if not trigger:
                continue
            ext = (b.close - e20) / atr if up else (e20 - b.close) / atr
            if ext > p.max_extension_atr:
                continue                                 # late entry, too extended from EMA20
            r = f.rsi[i]
            if (up and r < p.rsi_min_ce) or (not up and r > 100 - p.rsi_min_ce):
                continue
            swing = min(f.bars[k].low for k in range(i - self.PULLBACK_BARS, i + 1)) if up else \
                max(f.bars[k].high for k in range(i - self.PULLBACK_BARS, i + 1))
            ev = {"ema9": round(e9, 2), "ema20": round(e20, 2), "extension_atr": round(ext, 2), "rsi": round(r, 1),
                  "trigger": prev.high if up else prev.low}
            out.append(Candidate(f.index, self.id, direction, b.end, f"emapb:{day_key(f, i)}:{direction}:{swing:.2f}",
                                 directional_strength(f, i, direction), b.close, swing,
                                 (f"pullback into EMA 9/20 zone, close beyond prior {'high' if up else 'low'}",), ev))
        return out
