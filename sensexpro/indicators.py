"""Indicators on completed candles. Every function returns a list aligned with its input; positions inside the
warm-up period are None (never a made-up value). Pure functions: no I/O, no look-ahead (value i uses inputs 0..i).
"""
from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence

from .models import Bar

Series = list[Optional[float]]


def ema(values: Sequence[Optional[float]], n: int) -> Series:
    """Exponential moving average seeded with the simple average of the first n values."""
    out: Series = [None] * len(values)
    k = 2.0 / (n + 1)
    seed: list[float] = []
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
            continue
        prev = prev + k * (v - prev)
        out[i] = prev
    return out


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _wilder(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if i < n - 1:
            continue
        if prev is None:
            prev = sum(values[i - n + 1:i + 1]) / n
        else:
            prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag = sum(gains[1:n + 1]) / n
    al = sum(losses[1:n + 1]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i]) / n
            al = (al * (n - 1) + losses[i]) / n
        out[i] = 100.0 if al == 0 else (0.0 if ag == 0 else 100.0 - 100.0 / (1 + ag / al))
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist: Series = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> Series:
    return _wilder(true_ranges(bars), n)


def adx(bars: Sequence[Bar], n: int = 14) -> tuple[Series, Series, Series]:
    """(ADX, +DI, -DI), Wilder smoothing."""
    m = len(bars)
    adx_out: Series = [None] * m
    pdi_out: Series = [None] * m
    mdi_out: Series = [None] * m
    if m < 2 * n + 1:
        return adx_out, pdi_out, mdi_out
    tr = true_ranges(bars)
    pdm = [0.0] * m
    mdm = [0.0] * m
    for i in range(1, m):
        up = bars[i].high - bars[i - 1].high
        dn = bars[i - 1].low - bars[i].low
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx: Series = [None] * m
    for i in range(n, m):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
        if s_tr <= 0:
            continue
        p, q = 100 * s_p / s_tr, 100 * s_m / s_tr
        pdi_out[i], mdi_out[i] = p, q
        dx[i] = 0.0 if p + q == 0 else 100 * abs(p - q) / (p + q)
    first = [x for x in dx[n:2 * n] if x is not None]
    if len(first) < n:
        return adx_out, pdi_out, mdi_out
    a = sum(first) / n
    adx_out[2 * n - 1] = a
    for i in range(2 * n, m):
        if dx[i] is None:
            continue
        a = (a * (n - 1) + dx[i]) / n
        adx_out[i] = a
    return adx_out, pdi_out, mdi_out


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series, Series]:
    """(middle, upper, lower, bandwidth = (upper - lower) / middle)."""
    mid: Series = [None] * len(closes)
    up: Series = [None] * len(closes)
    lo: Series = [None] * len(closes)
    bw: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
        bw[i] = (2 * k * sd) / m if m else None
    return mid, up, lo, bw


def session_vwap(bars: Sequence[Bar]) -> tuple[Series, bool]:
    """VWAP reset at each IST session. Uses the bar volume (index future); if any bar of a session lacks volume, that
    session falls back to an equal-weighted typical price (returned flag True = fallback used somewhere)."""
    out: Series = [None] * len(bars)
    fallback = False
    i = 0
    while i < len(bars):
        day = bars[i].start.date()
        j = i
        while j < len(bars) and bars[j].start.date() == day:
            j += 1
        sess = bars[i:j]
        use_vol = all(b.volume is not None and b.volume > 0 for b in sess)
        fallback |= not use_vol
        pv = vv = 0.0
        for k, b in enumerate(sess):
            tp = (b.high + b.low + b.close) / 3
            w = b.volume if use_vol else 1.0
            pv += tp * w
            vv += w
            out[i + k] = pv / vv
        i = j
    return out, fallback


def volume_ratio(bars: Sequence[Bar], n: int = 20) -> Series:
    """Bar volume / median volume of the previous n bars (None if any is missing)."""
    out: Series = [None] * len(bars)
    for i in range(n, len(bars)):
        prev = [b.volume for b in bars[i - n:i]]
        cur = bars[i].volume
        if cur is None or any(v is None for v in prev):
            continue
        med = statistics.median(prev)
        if med and med > 0:
            out[i] = cur / med
    return out


def percentile_rank(values: Sequence[Optional[float]], window: int) -> Series:
    """Percentile (0-100) of value i among the previous `window` values (i excluded), None during warm-up."""
    out: Series = [None] * len(values)
    for i in range(window, len(values)):
        v = values[i]
        prev = [x for x in values[i - window:i] if x is not None]
        if v is None or len(prev) < window // 2:
            continue
        out[i] = 100.0 * sum(1 for x in prev if x <= v) / len(prev)
    return out


def slope(values: Sequence[Optional[float]], i: int, lookback: int) -> Optional[float]:
    """Average change per bar of values[i - lookback .. i]."""
    if i - lookback < 0:
        return None
    a, b = values[i - lookback], values[i]
    if a is None or b is None:
        return None
    return (b - a) / lookback
