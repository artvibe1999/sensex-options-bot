"""Trend validity and trap detection (daily bars and 5-minute bars).

A "valid" trend is a clean one-directional move: the efficiency ratio (net move / total path) is high, the fast
average sits on the right side of the slow one and the move is large enough to matter. A "trap" is a failed
breakout: price pushes beyond a recent high (or low), then closes back inside it. A failed upside breakout (bull
trap) is a bearish signal; a failed downside breakout (bear trap) is a bullish signal.

Pure functions, no I/O. They fail closed: with too little data the result is "no trend, no trap".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

BULL_TRAP, BEAR_TRAP = "BULL_TRAP", "BEAR_TRAP"


@dataclass(frozen=True)
class TrendState:
    dir: int = 0                    # +1 up, -1 down, 0 none / unclear
    valid: bool = False             # a clean single-direction move that is worth trading with
    trap: Optional[str] = None      # BULL_TRAP | BEAR_TRAP | None
    er: float = 0.0                 # efficiency ratio over the look-back (0 = chop, 1 = straight line)
    ready: bool = True              # False = not enough bars yet: "no trend" means UNKNOWN, not "range"

    @property
    def trap_dir(self) -> int:
        """Direction to trade after a trap: a failed upside breakout favours down, and vice versa."""
        return {BULL_TRAP: -1, BEAR_TRAP: 1}.get(self.trap or "", 0)


@dataclass(frozen=True)
class TrendInfo:
    daily: TrendState = TrendState()
    intraday: Optional[TrendState] = None


def _ema(xs: Sequence[float], n: int) -> float:
    k, v = 2.0 / (n + 1), xs[0]
    for x in xs[1:]:
        v = x * k + v * (1 - k)
    return v


def efficiency_ratio(closes: Sequence[float]) -> float:
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return abs(closes[-1] - closes[0]) / path if path > 0 else 0.0


def _trap(bars: Sequence[dict], ref_hi: float, ref_lo: float, last_close: float,
          breach: float, back: float) -> Optional[str]:
    """bars: the recent bars that may have broken the levels; last_close must be back inside by `back`."""
    up = any(b["high"] > ref_hi * (1 + breach) for b in bars) and last_close < ref_hi * (1 - back)
    dn = any(b["low"] < ref_lo * (1 - breach) for b in bars) and last_close > ref_lo * (1 + back)
    if up and not dn:
        return BULL_TRAP
    if dn and not up:
        return BEAR_TRAP
    return None                                   # both or neither: ambiguous chop


def daily_state(candles: Sequence[dict], look: int = 10, er_min: float = 0.35,
                breach: float = 0.0005, back: float = 0.0005) -> TrendState:
    """From COMPLETED daily candles (dicts with high/low/close)."""
    if len(candles) < 25:
        return TrendState(ready=False)
    closes = [c["close"] for c in candles]
    er = efficiency_ratio(closes[-(look + 1):])
    fast, slow = _ema(closes[-60:], 9), _ema(closes[-60:], 21)
    d = 1 if fast > slow * 1.0005 else (-1 if fast < slow * 0.9995 else 0)
    prior = candles[-(look + 1):-1]
    trap = _trap([candles[-1]], max(c["high"] for c in prior), min(c["low"] for c in prior),
                 closes[-1], breach, back)
    slope = closes[-1] - closes[-6]
    valid = trap is None and d != 0 and er >= er_min and (slope > 0) == (d > 0) and slope != 0
    return TrendState(d if d else 0, valid, trap, er)


def intraday_state(candles: Sequence[dict], spot: float, prior_high: Optional[float] = None,
                   prior_low: Optional[float] = None, look: int = 12, er_min: float = 0.40,
                   min_move: float = 0.002, breach: float = 0.0005, back: float = 0.0003) -> TrendState:
    """From 5-minute candles (oldest first; dicts with high/low/close), the live spot as the latest close,
    and the previous day's high/low. Pass TODAY's candles only; needs about 14 bars (about 70 minutes of session)."""
    if len(candles) < look + 2 or spot <= 0:
        return TrendState(ready=False)
    closes = [c["close"] for c in candles] + [spot]
    recent = closes[-(look + 1):]
    er = efficiency_ratio(recent)
    net = (recent[-1] - recent[0]) / recent[0]
    ema9 = _ema(closes[-40:], 9)
    d = 1 if net > 0 else (-1 if net < 0 else 0)
    last6 = list(candles[-6:]) + [{"high": spot, "low": spot, "close": spot}]
    older = candles[-36:-6] or candles[:-1]
    levels = [(max(c["high"] for c in older), min(c["low"] for c in older))]       # rolling range before the push
    if prior_high is not None and prior_low is not None:
        levels.append((prior_high, prior_low))                                       # previous day's range
    trap = None
    for hi, lo in levels:
        trap = _trap(last6, hi, lo, spot, breach, back)
        if trap:
            break
    valid = (trap is None and d != 0 and er >= er_min and abs(net) >= min_move
             and (spot > ema9) == (d > 0))
    return TrendState(d, valid, trap, er)
