"""Strategy 5: Bollinger Band squeeze breakout (brief 4.5). Bandwidth percentile is computed only from earlier bars."""
from __future__ import annotations

from types import SimpleNamespace

from .. import indicators as ind
from ..features import Frame
from .base import CE, PE, Candidate, day_key, directional_strength, macd_falling, macd_rising, ready


class BollingerSqueeze:
    id = "bollinger_squeeze"

    def __init__(self, p: SimpleNamespace) -> None:
        self.p = p
        self._pct_cache: tuple = (None, None)

    def _pct(self, f: Frame) -> list:
        frame, pct = self._pct_cache
        if frame is not f:                               # one Frame per index per bar: recompute for a new one
            pct = ind.percentile_rank(f.bb_bw, self.p.lookback)
            self._pct_cache = (f, pct)
        return pct

    def evaluate(self, f: Frame, i: int) -> list[Candidate]:
        p = self.p
        if i < p.squeeze_recent_bars + 1 or not ready(f, i, f.bb_up, f.bb_lo, f.bb_mid, f.vwap, f.atr) \
                or f.bb_up[i - 1] is None:
            return []
        pct = self._pct(f)
        recent = [pct[k] for k in range(i - p.squeeze_recent_bars, i) if pct[k] is not None]
        if not recent or min(recent) > p.squeeze_pct:
            return []                                    # no volatility contraction before this bar
        b, prev = f.bars[i], f.bars[i - 1]
        atr = f.atr[i]
        vr = f.vol_ratio[i]
        out = []
        for direction in (CE, PE):
            up = direction == CE
            outside = b.close > f.bb_up[i] if up else b.close < f.bb_lo[i]
            first = prev.close <= f.bb_up[i - 1] if up else prev.close >= f.bb_lo[i - 1]
            if not (outside and first):
                continue
            if (up and b.close <= f.vwap[i]) or (not up and b.close >= f.vwap[i]):
                continue
            if vr is None or vr < p.volume_mult:
                continue                                 # expansion needs volume
            h = f.macd_hist[i]
            mom = h is not None and ((h > 0 and macd_rising(f, i, 1)) if up else (h < 0 and macd_falling(f, i, 1)))
            r = f.rsi[i]
            mom = bool(mom) or (r is not None and (r > 55 if up else r < 45))
            if not mom:
                continue
            if f.strong_against(i, direction):
                continue                                 # breakout against a strong higher-timeframe trend
            ext = (b.close - f.bb_up[i]) / atr if up else (f.bb_lo[i] - b.close) / atr
            if ext > p.max_extension_atr:
                continue
            ev = {"bandwidth_pct_min": round(min(recent), 1), "band": round(f.bb_up[i] if up else f.bb_lo[i], 2),
                  "basis": round(f.bb_mid[i], 2), "extension_atr": round(ext, 2), "vol_ratio": round(vr, 2)}
            out.append(Candidate(f.index, self.id, direction, b.end, f"sq:{day_key(f, i)}:{direction}:{b.start:%H%M}",
                                 directional_strength(f, i, direction), b.close, f.bb_mid[i],
                                 (f"squeeze released: close {'above upper' if up else 'below lower'} band",), ev,
                                 strong=vr >= max(1.5, p.volume_mult)))
        return out
