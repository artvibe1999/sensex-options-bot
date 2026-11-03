"""Transparent 100-point signal score (brief 4.7).

| component            | weight | how                                                                       |
|----------------------|-------:|---------------------------------------------------------------------------|
| trend15              |     20 | 15-min EMA structure aligned 20, mixed 8, opposed 0                       |
| vwap                 |     20 | close on the trade's side of VWAP 10 + VWAP slope in its direction 10 / 5 |
| price_action         |     20 | the strategy's own confirmation strength x 20                             |
| volume               |     15 | future-volume ratio >= 1.5: 15, >= 1.2: 11, >= 1.0: 6; missing: 0         |
| momentum             |     15 | RSI side 7 / 4 + MACD histogram side and rising 8 / 4                     |
| option_liquidity     |     10 | set after contract selection: spread <= 0.5%: 10, <= 1%: 7, <= 1.5%: 4    |

Correlation note: VWAP position and price action both look at the close, so the VWAP part scores position and slope
only (not distance), and momentum uses RSI + MACD histogram, not a third trend measure. Missing data scores 0.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from ..features import Frame
from ..models import CE, Candidate


def option_liquidity_points(spread_pct: Optional[float]) -> float:
    if spread_pct is None:
        return 0.0
    if spread_pct <= 0.5:
        return 10.0
    if spread_pct <= 1.0:
        return 7.0
    if spread_pct <= 1.5:
        return 4.0
    return 0.0


def score(c: Candidate, f: Frame, i: int, regime_p: SimpleNamespace,
          liquidity: Optional[float] = None) -> tuple[float, dict, list[str]]:
    up = c.direction == CE
    comp: dict[str, float] = {}
    why: list[str] = []

    t = f.trend15(i)
    want = 1 if up else -1
    comp["trend15"] = 20.0 if t == want else (8.0 if t == 0 else 0.0)
    why.append(f"15m trend {'aligned' if t == want else 'mixed' if t == 0 else 'opposed'}")

    vwap, atr = f.vwap[i], f.atr[i]
    slope = f.vwap_slope(i)
    pts = 0.0
    if vwap is not None:
        right_side = c.ref_price > vwap if up else c.ref_price < vwap
        pts += 10.0 if right_side else 0.0
    if slope is not None and atr:
        s = slope / atr * (1 if up else -1)
        pts += 10.0 if s >= regime_p.vwap_slope_atr else (5.0 if s > 0 else 0.0)
    comp["vwap"] = pts
    why.append(f"VWAP {pts:.0f}/20")

    comp["price_action"] = round(20.0 * max(0.0, min(1.0, c.price_action)), 1)

    vr = f.vol_ratio[i]
    comp["volume"] = 0.0 if vr is None else (15.0 if vr >= 1.5 else 11.0 if vr >= 1.2 else 6.0 if vr >= 1.0 else 0.0)
    why.append("volume unavailable" if vr is None else f"volume x{vr:.2f}")

    m = 0.0
    r = f.rsi[i]
    if r is not None:
        rs = r if up else 100 - r
        m += 7.0 if rs > 55 else (4.0 if rs > 50 else 0.0)
    h = f.macd_hist[i]
    h0 = f.macd_hist[i - 1] if i > 0 else None
    if h is not None:
        hs = h if up else -h
        rising = h0 is not None and ((h > h0) if up else (h < h0))
        m += 8.0 if hs > 0 and rising else (4.0 if hs > 0 else 0.0)
    comp["momentum"] = m

    comp["option_liquidity"] = 0.0 if liquidity is None else float(liquidity)
    total = round(sum(comp.values()), 1)
    return total, comp, why
