"""Strategy 2: VWAP trend continuation (brief 4.2). Pullback toward VWAP / EMA9 in an established intraday trend,
entered only on a completed confirmation candle (a touch alone never triggers)."""
from __future__ import annotations

from types import SimpleNamespace

from ..features import Frame
from .base import CE, PE, Candidate, day_key, directional_strength, macd_falling, macd_rising, ready


class VwapContinuation:
    id = "vwap_continuation"
    PULLBACK_BARS = 3

    def __init__(self, p: SimpleNamespace) -> None:
        self.p = p

    def evaluate(self, f: Frame, i: int) -> list[Candidate]:
        p = self.p
        d0 = f.day_start(i)
        if i - d0 < self.PULLBACK_BARS + 1 or not ready(f, i, f.vwap, f.ema_f, f.ema_s, f.atr, f.rsi):
            return []
        b, prev = f.bars[i], f.bars[i - 1]
        vwap, atr = f.vwap[i], f.atr[i]
        slope = f.vwap_slope(i)
        crosses = f.vwap_crosses(i)
        if slope is None or crosses > p.max_vwap_crosses:
            return []                                    # repeated VWAP-crossing range session
        t15 = f.trend15(i)
        out = []
        for direction in (CE, PE):
            up = direction == CE
            if up and not (b.close > vwap and slope > 0 and f.ema_f[i] > f.ema_s[i] and t15 == 1):
                continue
            if not up and not (b.close < vwap and slope < 0 and f.ema_f[i] < f.ema_s[i] and t15 == -1):
                continue
            seg = range(i - self.PULLBACK_BARS, i + 1)
            # pullback: some recent bar reached the VWAP / EMA9 zone ...
            touched = False
            for k in seg:
                if f.vwap[k] is None or f.ema_f[k] is None:
                    continue
                zone = max(f.vwap[k], f.ema_f[k]) if up else min(f.vwap[k], f.ema_f[k])
                if (up and f.bars[k].low <= zone + p.pullback_atr * atr) or \
                        (not up and f.bars[k].high >= zone - p.pullback_atr * atr):
                    touched = True
            # ... without a close through VWAP (structure intact)
            intact = all(f.vwap[k] is not None and ((f.bars[k].close > f.vwap[k]) if up else (f.bars[k].close < f.vwap[k]))
                         for k in seg)
            if not (touched and intact):
                continue
            # completed confirmation candle back in the trend direction
            confirm = (b.close > b.open and b.close > prev.close) if up else (b.close < b.open and b.close < prev.close)
            if not confirm:
                continue
            r = f.rsi[i]
            if (up and r < p.rsi_min_ce) or (not up and r > 100 - p.rsi_min_ce):
                continue                                 # momentum deteriorated
            if (up and macd_falling(f, i, 3)) or (not up and macd_rising(f, i, 3)):
                continue
            vr = f.vol_ratio[i]
            if vr is not None and vr < 0.8:
                continue                                 # volume dried up on the confirmation candle
            swing = min(f.bars[k].low for k in seg) if up else max(f.bars[k].high for k in seg)
            ev = {"vwap": round(vwap, 2), "ema9": round(f.ema_f[i], 2), "ema20": round(f.ema_s[i], 2),
                  "vwap_crosses": crosses, "rsi": round(r, 1), "pullback_extreme": swing,
                  "vol_ratio": None if vr is None else round(vr, 2)}
            out.append(Candidate(f.index, self.id, direction, b.end,
                                 f"vwapc:{day_key(f, i)}:{direction}:{swing:.2f}",
                                 directional_strength(f, i, direction), b.close, swing,
                                 (f"pullback to VWAP/EMA9 held, confirmation candle closed {'up' if up else 'down'}",),
                                 ev))
        return out
