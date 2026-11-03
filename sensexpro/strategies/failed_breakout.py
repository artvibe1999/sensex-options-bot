"""Strategy 4: failed breakout / liquidity sweep reversal (brief 4.4).

Objective pattern (no "every wick is a sweep"):
  1. a reference level: opening-range high/low, previous session high/low, or a confirmed intraday swing (a pivot with
     2 lower lows / higher highs on each side);
  2. the bar before the sweep closed on the level's holding side;
  3. a sweep bar trades beyond the level by at least min_sweep_atr x ATR;
  4. within max_sweep_bars, a COMPLETED candle closes back on the holding side, in the reversal direction, with
     momentum turning (RSI or MACD histogram). Entry is after that confirmation, never on the first wick.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from ..features import Frame
from .base import CE, PE, Candidate, day_key, directional_strength, ready

PRIORITY = {"prev_session": 0, "opening_range": 1, "swing": 2}


class FailedBreakout:
    id = "failed_breakout"

    def __init__(self, p: SimpleNamespace) -> None:
        self.p = p

    def _levels(self, f: Frame, i: int) -> list[tuple[str, float, str]]:
        """(name, price, side) with side 'support' (CE setups) or 'resistance' (PE setups)."""
        out: list[tuple[str, float, str]] = []
        psh = f.prev_session_hl(i)
        if psh:
            out += [("prev_session", psh[1], "support"), ("prev_session", psh[0], "resistance")]
        orr = f.opening_range(i)
        if orr and f.bars[i].end > orr[2]:
            out += [("opening_range", orr[1], "support"), ("opening_range", orr[0], "resistance")]
        d0 = f.day_start(i)
        lo_k = max(d0 + 2, i - self.p.swing_lookback)
        hi_k = i - self.p.max_sweep_bars - 2
        for k in range(lo_k, hi_k):
            b = f.bars[k]
            nb = [f.bars[k - 2], f.bars[k - 1], f.bars[k + 1], f.bars[k + 2]]
            if all(b.low < x.low for x in nb):
                out.append(("swing", b.low, "support"))
            if all(b.high > x.high for x in nb):
                out.append(("swing", b.high, "resistance"))
        return out

    def _sweep(self, f: Frame, i: int, level: float, up: bool, atr: float) -> Optional[int]:
        """Index of the sweep bar for this level, or None."""
        d0 = f.day_start(i)
        for s in range(max(d0 + 1, i - self.p.max_sweep_bars), i):
            sb, before = f.bars[s], f.bars[s - 1]
            if up and before.close > level and sb.low < level - self.p.min_sweep_atr * atr:
                return s
            if not up and before.close < level and sb.high > level + self.p.min_sweep_atr * atr:
                return s
        return None

    def evaluate(self, f: Frame, i: int) -> list[Candidate]:
        p = self.p
        if i < 2 or not ready(f, i, f.atr, f.vwap, f.rsi) or f.rsi[i - 1] is None:
            return []
        b, prev = f.bars[i], f.bars[i - 1]
        atr = f.atr[i]
        out = []
        for direction, side in ((CE, "support"), (PE, "resistance")):
            up = direction == CE
            best = None
            for name, level, sd in self._levels(f, i):
                if sd != side:
                    continue
                s = self._sweep(f, i, level, up, atr)
                if s is None:
                    continue
                reclaimed = b.close > level if up else b.close < level
                turn = (b.close > b.open and b.close > prev.close) if up else (b.close < b.open and b.close < prev.close)
                if not (reclaimed and turn):
                    continue
                if best is None or PRIORITY[name] < PRIORITY[best[0]]:
                    best = (name, level, s)
            if best is None:
                continue
            name, level, s = best
            h = f.macd_hist
            mom = (f.rsi[i] > f.rsi[i - 1]) if up else (f.rsi[i] < f.rsi[i - 1])
            if h[i] is not None and h[i - 1] is not None:
                mom = mom or ((h[i] > h[i - 1]) if up else (h[i] < h[i - 1]))
            if not mom:
                continue                                 # no momentum reversal evidence
            vr = f.vol_ratio[i]
            j = f.j15(i)
            adx = f.adx15[j] if j is not None else None
            t15 = f.trend15(i)
            fading_trend = adx is not None and adx >= p.strong_trend_adx and t15 == (-1 if up else 1)
            if fading_trend and (vr is None or vr < 1.5):
                continue                                 # strong trend day: do not fade without volume evidence
            extreme = min(x.low for x in f.bars[s:i + 1]) if up else max(x.high for x in f.bars[s:i + 1])
            strength = directional_strength(f, i, direction)
            vwap_ok = b.close > f.vwap[i] if up else b.close < f.vwap[i]
            strong = vwap_ok and strength >= 0.7 and (vr is None or vr >= 1.2)
            ev = {"level": name, "level_price": level, "sweep_bar": f.bars[s].start.isoformat(),
                  "sweep_extreme": extreme, "vwap_reclaimed": vwap_ok,
                  "vol_ratio": None if vr is None else round(vr, 2), "adx15": None if adx is None else round(adx, 1)}
            out.append(Candidate(
                f.index, self.id, direction, b.end, f"fb:{day_key(f, i)}:{direction}:{name}:{f.bars[s].start:%H%M}",
                strength if vwap_ok else strength * 0.8, b.close, extreme,
                (f"failed {'breakdown' if up else 'breakout'} of {name.replace('_', ' ')} {level:.2f}, reclaimed",),
                ev, strong=strong))
        return out
