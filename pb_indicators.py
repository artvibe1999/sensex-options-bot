"""Pure-Python indicators for the playbook (same formulas as the research lab's prep.py, so live signals match the
backtest): EMA, Wilder RMA/ATR, RSI, ADX/+DI/-DI, Supertrend, MACD histogram, 15-minute bars from 5-minute bars."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Sequence

NAN = float("nan")


@dataclass(frozen=True)
class Candle:
    ts: dt.datetime          # bar START time
    open: float
    high: float
    low: float
    close: float


def ema(x: Sequence[float], n: int) -> list[float]:
    if not x:
        return []
    a = 2.0 / (n + 1)
    out = [x[0]]
    for v in x[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def rma(x: Sequence[float], n: int) -> list[float]:
    out = [NAN] * len(x)
    if len(x) < n:
        return out
    seed = [v for v in x[:n] if not math.isnan(v)]
    out[n - 1] = sum(seed) / len(seed) if seed else NAN
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def true_range(h, l, c) -> list[float]:
    out = []
    for i in range(len(c)):
        pc = c[i - 1] if i else c[0]
        out.append(max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc)))
    return out


def atr(h, l, c, n=14) -> list[float]:
    return rma(true_range(h, l, c), n)


def rsi(c, n=14) -> list[float]:
    d = [0.0] + [c[i] - c[i - 1] for i in range(1, len(c))]
    up = rma([max(v, 0.0) for v in d], n)
    dn = rma([max(-v, 0.0) for v in d], n)
    return [100 - 100 / (1 + u / (q if q else 1e-9)) if not (math.isnan(u) or math.isnan(q)) else NAN
            for u, q in zip(up, dn)]


def adx(h, l, c, n=14) -> tuple[list[float], list[float], list[float]]:
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(c)):
        uh, dl = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(uh if (uh > dl and uh > 0) else 0.0)
        mdm.append(dl if (dl > uh and dl > 0) else 0.0)
    a = atr(h, l, c, n)
    rp, rm = rma(pdm, n), rma(mdm, n)
    pdi = [100 * p / q if q and not math.isnan(q) else NAN for p, q in zip(rp, a)]
    mdi = [100 * p / q if q and not math.isnan(q) else NAN for p, q in zip(rm, a)]
    dx = []
    for p, m in zip(pdi, mdi):
        if math.isnan(p) or math.isnan(m):
            dx.append(0.0)                      # lab: nan_to_num(dx)
        else:
            s = p + m
            dx.append(100 * abs(p - m) / (s if s else 1e-9))
    return rma(dx, n), pdi, mdi


def supertrend(h, l, c, n=10, m=3.0) -> tuple[list[int], list[float]]:
    a = atr(h, l, c, n)
    ub = [(hh + ll) / 2 + m * x for hh, ll, x in zip(h, l, a)]
    lb = [(hh + ll) / 2 - m * x for hh, ll, x in zip(h, l, a)]
    fu, fl = ub[:], lb[:]
    d = [1] * len(c)
    line = [NAN] * len(c)
    for i in range(1, len(c)):
        if math.isnan(a[i]):
            continue
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1] or math.isnan(fu[i - 1])) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1] or math.isnan(fl[i - 1])) else fl[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if c[i] < fl[i] else 1
        else:
            d[i] = 1 if c[i] > fu[i] else -1
        line[i] = fl[i] if d[i] == 1 else fu[i]
    return d, line


def macd_hist(c, fast=12, slow=26, sig=9) -> list[float]:
    m = [a - b for a, b in zip(ema(c, fast), ema(c, slow))]
    return [a - b for a, b in zip(m, ema(m, sig))]


def to_15m(bars5: Sequence[Candle]) -> list[Candle]:
    """Aggregate 5-minute bars into 15-minute bars aligned at 09:15 (09:15, 09:30, ...). Partial buckets are kept;
    the caller decides which ones are complete."""
    out: list[Candle] = []
    key = None
    for b in bars5:
        mins = b.ts.hour * 60 + b.ts.minute - (9 * 60 + 15)
        start = b.ts.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=9 * 60 + 15 + (mins // 15) * 15)
        if start != key:
            out.append(Candle(start, b.open, b.high, b.low, b.close))
            key = start
        else:
            p = out[-1]
            out[-1] = Candle(p.ts, p.open, max(p.high, b.high), min(p.low, b.low), b.close)
    return out
