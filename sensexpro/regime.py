"""Market regime per index (brief 4.6). A filter, not an oracle: strategies still need their own conditions.

Order of precedence: DATA_UNSAFE, LOW_LIQUIDITY_OR_UNCERTAIN, HIGH_VOLATILITY, TREND_UP / TREND_DOWN, RANGE.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from .features import Frame

TREND_UP, TREND_DOWN, RANGE = "TREND_UP", "TREND_DOWN", "RANGE"
HIGH_VOL, LOW_LIQ, DATA_UNSAFE = "HIGH_VOLATILITY", "LOW_LIQUIDITY_OR_UNCERTAIN", "DATA_UNSAFE"
REGIMES = (TREND_UP, TREND_DOWN, RANGE, HIGH_VOL, LOW_LIQ, DATA_UNSAFE)

# Brief 4.6 permission table. Values: "CE" / "PE" = only that side; "both"; "block"; "strong" variants and
# "selective" are refined in the ensemble (stricter score / strong evidence).
PERMISSIONS = {
    TREND_UP: {"orb": "CE", "vwap_continuation": "CE", "ema_pullback": "CE", "failed_breakout": "CE_strong",
               "bollinger_squeeze": "CE"},
    TREND_DOWN: {"orb": "PE", "vwap_continuation": "PE", "ema_pullback": "PE", "failed_breakout": "PE_strong",
                 "bollinger_squeeze": "PE"},
    RANGE: {"orb": "block", "vwap_continuation": "block", "ema_pullback": "block", "failed_breakout": "both",
            "bollinger_squeeze": "confirmed"},
    HIGH_VOL: {s: "selective" for s in ("orb", "vwap_continuation", "ema_pullback", "failed_breakout",
                                         "bollinger_squeeze")},
    LOW_LIQ: {s: "block" for s in ("orb", "vwap_continuation", "ema_pullback", "failed_breakout", "bollinger_squeeze")},
    DATA_UNSAFE: {s: "block" for s in ("orb", "vwap_continuation", "ema_pullback", "failed_breakout",
                                        "bollinger_squeeze")},
}


@dataclass(frozen=True)
class RegimeState:
    regime: str
    evidence: dict


def detect(f: Frame, i: int, p: SimpleNamespace, data_ok: bool = True, liquidity_ok: bool = True,
           why_unsafe: str = "") -> RegimeState:
    ev: dict = {}
    if not data_ok:
        return RegimeState(DATA_UNSAFE, {"why": why_unsafe or "data feed not safe"})
    if len(f.bars15) < p.min_warmup_bars_15m or f.atr[i] is None or f.vwap[i] is None:
        return RegimeState(DATA_UNSAFE, {"why": f"warm-up incomplete ({len(f.bars15)} x 15m bars)"})
    if f.has_gap(i):
        return RegimeState(DATA_UNSAFE, {"why": "missing 1-minute candles in the last 30 minutes"})
    if not liquidity_ok:
        return RegimeState(LOW_LIQ, {"why": "near-the-money options not quoted / too wide"})
    ratio = f.atr_ratio(i)
    ev["atr_ratio"] = None if ratio is None else round(ratio, 2)
    if ratio is not None and ratio >= p.high_vol_atr_ratio:
        return RegimeState(HIGH_VOL, ev)
    j = f.j15(i)
    adx = f.adx15[j] if j is not None else None
    slope = f.vwap_slope(i)
    atr = f.atr[i]
    crosses = f.vwap_crosses(i)
    t15 = f.trend15(i)
    ev.update(adx15=None if adx is None else round(adx, 1), trend15=t15, vwap_crosses=crosses,
              vwap_slope_atr=None if slope is None else round(slope / atr, 3))
    trending = adx is not None and adx >= p.adx_trend and crosses <= p.max_vwap_crosses_trend
    if trending and slope is not None:
        s = slope / atr
        if t15 == 1 and s >= p.vwap_slope_atr:
            return RegimeState(TREND_UP, ev)
        if t15 == -1 and s <= -p.vwap_slope_atr:
            return RegimeState(TREND_DOWN, ev)
    return RegimeState(RANGE, ev)
