"""Strategy 1: Opening Range Breakout (brief 4.1)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from ..features import Frame
from .base import CE, PE, Candidate, day_key, directional_strength, ready


class OpeningRangeBreakout:
    id = "orb"

    def __init__(self, p: SimpleNamespace) -> None:
        self.p = p
        h, m = str(p.last_entry).split(":")
        self.last_entry = dt.time(int(h), int(m))

    def evaluate(self, f: Frame, i: int) -> list[Candidate]:
        p = self.p
        orr = f.opening_range(i, p.range_minutes)
        if orr is None or not ready(f, i, f.vwap, f.atr):
            return []
        hi, lo, end = orr
        b = f.bars[i]
        if b.end <= end or b.end.time() > self.last_entry:
            return []                                    # never before the range is complete
        d0 = f.day_start(i)
        prev_close = f.bars[i - 1].close if i > d0 else None
        out = []
        for direction, level, broke in ((CE, hi, b.close > hi), (PE, lo, b.close < lo)):
            if not broke or prev_close is None:
                continue
            fresh = prev_close <= hi if direction == CE else prev_close >= lo
            if not fresh:
                continue                                 # already outside: not a new breakout candle
            ev = {"or_high": hi, "or_low": lo, "close": b.close}
            # whipsaw: earlier breakout closes on this side today that fell back inside the range
            attempts = 0
            inside_prev = True
            for k in range(d0, i):
                c = f.bars[k].close
                outside = c > hi if direction == CE else c < lo
                if outside and inside_prev and f.bars[k].end > end:
                    attempts += 1
                inside_prev = not outside
            ev["prior_attempts"] = attempts
            if attempts + 1 > p.max_attempts:
                continue                                 # repeated whipsaw breakouts: rejected
            vwap, atr = f.vwap[i], f.atr[i]
            slope = f.vwap_slope(i)
            vr = f.vol_ratio[i]
            ev.update(vwap=round(vwap, 2), vwap_slope=None if slope is None else round(slope, 3),
                      vol_ratio=None if vr is None else round(vr, 2), bar_range_atr=round(b.range / atr, 2))
            if direction == CE and not (b.close > vwap and slope is not None and slope > 0):
                continue
            if direction == PE and not (b.close < vwap and slope is not None and slope < 0):
                continue
            if vr is None or vr < p.volume_mult:
                continue                                 # low-volume (or unknown-volume) break: rejected
            if f.strong_against(i, direction):
                continue
            if b.range > p.max_breakout_atr * atr:
                continue                                 # chasing an abnormally large candle: wait / reject
            inval = b.low if direction == CE else b.high
            out.append(Candidate(
                f.index, self.id, direction, b.end, f"orb:{day_key(f, i)}:{direction}:{attempts + 1}",
                directional_strength(f, i, direction), b.close, inval,
                (f"close {'above' if direction == CE else 'below'} opening range {'high' if direction == CE else 'low'} "
                 f"{level:.2f}",), ev))
        return out
