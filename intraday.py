"""Intraday price-action signals on 5-minute SENSEX bars: support / resistance, sideways-day filter, breakouts and
traps. Shared by `intraday_backtest.py` and the live bot so both see exactly the same signal.

Levels (support / resistance)
  prior day high / low / close, the Central Pivot Range (pivot, BC, TC), the opening range (first `or_bars` bars)
  and today's swing highs / lows (5-bar fractals).

Sideways filter ("do not buy options on a dead day")
  * a wide CPR (> `cpr_wide_pct` of price) usually precedes a range day; a narrow one precedes trend days;
  * the efficiency ratio (net move / path) of the last `er_look` bars must be >= `er_min`;
  * the day must already have expanded: today's range >= `min_range_adr` x the 10-day average daily range;
  * the whole session so far must be directional: efficiency ratio since the open >= `day_er_min`.
  On 2023-2026 5-minute data every one of these "dead day" conditions lost money in BOTH halves of the sample.

BREAKOUT (trend continuation)
  a bar CLOSES beyond a key level by `buffer_pct`, with a strong body, room to the next level of at least `room_r` x the
  risk, and (with `confirm=1`) the next bar still holding beyond the level. Stop: back inside the level.

TRAP (failed breakout / stop hunt): what large players do to breakout buyers
  within the last `trap_bars` bars price poked beyond a key level, retail breakout orders and stops fired, then a bar
  closed back inside with an opposite-colour body. Trade the reversal; stop just beyond the poke extreme.

PULLBACK (trend continuation after a breakout)
  a confirmed breakout happened earlier today (within `pb_look` bars); price then came back to the broken level
  (within `pb_tol`) without closing back through the stop, and the latest bar closed strongly in the breakout
  direction beyond the previous bar's extreme. Stop: below the pullback low (above the high for shorts).

RANGE (for premium sellers only)
  after `range_after`, a low-efficiency day still inside its opening range and well inside its average range.

Nothing here is a proven edge on its own. `intraday_backtest.py` measures every variant walk-forward on years of
5-minute data, and only variants that hold up out of sample are approved.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional, Sequence

BREAKOUT, TRAP, RANGE, PULLBACK = "BREAKOUT", "TRAP", "RANGE", "PULLBACK"


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime          # bar START time (Kite convention); the bar is complete at ts + 5 min
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Levels:
    pdh: float
    pdl: float
    pdc: float
    pivot: float
    bc: float
    tc: float
    cpr_pct: float           # |TC - BC| / pivot x 100
    adr: float               # average daily range (points) of the previous days
    orh: Optional[float] = None
    orl: Optional[float] = None


@dataclass(frozen=True)
class SignalParams:
    or_bars: int = 6                  # opening range = first 30 minutes
    buffer_pct: float = 0.0005        # a close must clear the level by 0.05% (~37 points at 74k)
    stop_buffer_pct: float = 0.0010   # breakout stop: 0.10% back inside the level
    confirm: int = 1                  # 1: the bar after the breakout must still close beyond the level
    min_body: float = 0.5             # breakout bar body >= this share of its range
    er_look: int = 12                 # efficiency ratio over the last hour
    er_min: float = 0.30
    cpr_wide_pct: float = 0.0         # > 0: skip trend trades when CPR width exceeds this (% of price)
    min_range_adr: float = 0.0        # > 0: the day's range so far must be >= this x ADR
    day_er_min: float = 0.0           # > 0: efficiency ratio of the whole session so far must be >= this
    room_r: float = 1.5               # next level must be at least this many risk-units away
    target_r: float = 2.0             # target = entry +/- target_r x risk
    trap_bars: int = 3                # the poke beyond the level happened within this many bars
    pb_look: int = 18                 # pullback: the breakout happened within the last 90 minutes
    pb_tol: float = 0.0015            # pullback: came back to within 0.15% of the broken level
    range_after: dt.time = dt.time(11, 0)
    range_er_max: float = 0.15
    range_adr_max: float = 0.6
    entry_start: dt.time = dt.time(9, 45)
    entry_end: dt.time = dt.time(14, 0)


@dataclass(frozen=True)
class Signal:
    kind: str                # BREAKOUT | TRAP | RANGE
    direction: int           # +1 bullish, -1 bearish, 0 range
    level: float             # the level broken / defended (RANGE: midpoint)
    entry: float             # underlying at the signal bar's close
    stop: float              # underlying level that invalidates the idea
    target: float            # underlying target (RANGE: unused, = entry)
    ts: dt.datetime          # signal bar START time
    reason: str
    hi: float = 0.0          # RANGE: the day's high / low so far (short strikes go beyond them)
    lo: float = 0.0

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


# ---------------------------------------------------------------- levels
def levels_from(prev_days: Sequence[Sequence[Bar]], today: Sequence[Bar], or_bars: int) -> Optional[Levels]:
    """prev_days: earlier sessions (oldest first, at least the previous one). today: completed bars of today."""
    if not prev_days or not prev_days[-1]:
        return None
    y = prev_days[-1]
    h, l, c = max(b.high for b in y), min(b.low for b in y), y[-1].close
    p = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2 * p - bc
    ranges = [max(b.high for b in d) - min(b.low for b in d) for d in prev_days[-10:] if d]
    orh = orl = None
    if len(today) >= or_bars:
        orh, orl = max(b.high for b in today[:or_bars]), min(b.low for b in today[:or_bars])
    return Levels(h, l, c, p, min(bc, tc), max(bc, tc), abs(tc - bc) / p * 100.0,
                  sum(ranges) / len(ranges), orh, orl)


def levels_from_daily(prev: Sequence[dict], today: Sequence[Bar], or_bars: int) -> Optional[Levels]:
    """Same as levels_from, from completed DAILY candles (dicts with high/low/close), as the live bot has them."""
    if not prev:
        return None
    y = prev[-1]
    h, l, c = float(y["high"]), float(y["low"]), float(y["close"])
    p = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2 * p - bc
    ranges = [float(d["high"]) - float(d["low"]) for d in prev[-10:]]
    orh = orl = None
    if len(today) >= or_bars:
        orh, orl = max(b.high for b in today[:or_bars]), min(b.low for b in today[:or_bars])
    return Levels(h, l, c, p, min(bc, tc), max(bc, tc), abs(tc - bc) / p * 100.0,
                  sum(ranges) / len(ranges), orh, orl)


def swing_levels(bars: Sequence[Bar], k: int = 2) -> tuple[list[float], list[float]]:
    """Fractal swing highs and lows: a bar whose high (low) is the extreme of the k bars on each side."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i].high == max(b.high for b in win):
            highs.append(bars[i].high)
        if bars[i].low == min(b.low for b in win):
            lows.append(bars[i].low)
    return highs, lows


def key_levels(lv: Levels, bars: Sequence[Bar]) -> list[float]:
    out = [lv.pdh, lv.pdl, lv.pdc, lv.pivot, lv.bc, lv.tc]
    if lv.orh is not None:
        out += [lv.orh, lv.orl]
    hs, ls = swing_levels(bars)
    return sorted(set(round(x, 2) for x in out + hs + ls))


def efficiency(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return 0.0
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return abs(closes[-1] - closes[0]) / path if path > 0 else 0.0


def sideways(bars: Sequence[Bar], lv: Levels, p: SignalParams) -> tuple[bool, str]:
    """True (with the reason) when the day looks range-bound: no trend trades then."""
    if p.cpr_wide_pct > 0 and lv.cpr_pct > p.cpr_wide_pct:
        return True, f"wide CPR {lv.cpr_pct:.2f}%"
    closes = [b.close for b in bars[-(p.er_look + 1):]]
    er = efficiency(closes)
    if er < p.er_min:
        return True, f"choppy (ER {er:.2f})"
    if p.min_range_adr > 0 and lv.adr > 0:
        rng = max(b.high for b in bars) - min(b.low for b in bars)
        if rng < p.min_range_adr * lv.adr:
            return True, f"narrow day ({rng / lv.adr:.2f} x ADR)"
    if p.day_er_min > 0:
        der = efficiency([bars[0].open] + [b.close for b in bars])
        if der < p.day_er_min:
            return True, f"choppy session (ER {der:.2f})"
    return False, ""


# ---------------------------------------------------------------- detection
def _bar_end(b: Bar) -> dt.time:
    return (b.ts + dt.timedelta(minutes=5)).time()


def _breakout_at(bars: Sequence[Bar], i: int, levels: list[float], p: SignalParams) -> Optional[tuple[int, float]]:
    """(direction, level) if bar i closed beyond a key level that the previous bar closed inside of."""
    b, prev = bars[i], bars[i - 1]
    rng = b.high - b.low
    if rng <= 0 or abs(b.close - b.open) < p.min_body * rng:
        return None
    up = [L for L in levels if prev.close <= L < b.close / (1 + p.buffer_pct)]
    dn = [L for L in levels if prev.close >= L > b.close / (1 - p.buffer_pct)]
    if up and b.close > b.open and b.close >= b.high - 0.3 * rng:
        return 1, max(up)
    if dn and b.close < b.open and b.close <= b.low + 0.3 * rng:
        return -1, min(dn)
    return None


def detect(bars: Sequence[Bar], lv: Levels, p: SignalParams, kind: str,
           kl: Optional[Sequence[list[float]]] = None) -> Optional[Signal]:
    """Signal on the LAST completed bar of `bars` (today only, oldest first), or None.

    kl: optional precomputed key levels, kl[i] == key_levels(lv, bars[:i]) (the backtest caches them)."""
    levels_upto = (lambda i: kl[i]) if kl is not None else (lambda i: key_levels(lv, bars[:i]))
    n = len(bars)
    if n < max(p.or_bars + 1, 3) or lv.orh is None:
        return None
    last = bars[-1]
    if not (p.entry_start <= _bar_end(last) <= p.entry_end):
        return None
    levels = levels_upto(n - 1)                      # levels known BEFORE the signal bar
    if kind == BREAKOUT:
        side, why = sideways(bars, lv, p)
        if side:
            return None
        i = n - 1 - (1 if p.confirm else 0)
        if i < p.or_bars:
            return None
        hit = _breakout_at(bars, i, levels_upto(i), p)
        if hit is None:
            return None
        d, L = hit
        if p.confirm and (d * (last.close - L) <= 0 or d * (last.close - last.open) < 0):
            return None                              # the follow-up bar fell back or turned: likely a trap
        stop = L * (1 - d * p.stop_buffer_pct)
        risk = abs(last.close - stop)
        if risk <= 0:
            return None
        beyond = [x for x in levels if d * (x - last.close) > 0]
        nxt = min(beyond, key=lambda x: abs(x - last.close)) if beyond else None
        if nxt is not None and abs(nxt - last.close) < p.room_r * risk:
            return None                              # next support/resistance too close: no room to run
        return Signal(BREAKOUT, d, L, last.close, stop, last.close + d * p.target_r * risk, last.ts,
                      f"{'up' if d > 0 else 'down'} breakout of {L:.0f}" + (" (confirmed)" if p.confirm else ""))
    if kind == TRAP:
        if n < p.or_bars + 2:
            return None
        win = bars[-(p.trap_bars + 1):-1]
        before = bars[:-(p.trap_bars + 1)]
        if not win or not before:
            return None
        ref = levels_upto(len(before))
        hi, lo = max(b.high for b in win), min(b.low for b in win)
        base = before[-1].close
        # bull trap: a level above where price stood, poked through, now closed back below with a red bar
        bull = [L for L in ref if base < L and hi > L * (1 + p.buffer_pct) and last.close < L and last.close < last.open]
        bear = [L for L in ref if base > L and lo < L * (1 - p.buffer_pct) and last.close > L and last.close > last.open]
        if bull:
            L = max(bull)
            stop = hi * (1 + p.buffer_pct)
            risk = stop - last.close
            if risk > 0:
                return Signal(TRAP, -1, L, last.close, stop, last.close - p.target_r * risk, last.ts,
                              f"bull trap above {L:.0f} (high {hi:.0f}, closed back below)")
        if bear:
            L = min(bear)
            stop = lo * (1 - p.buffer_pct)
            risk = last.close - stop
            if risk > 0:
                return Signal(TRAP, 1, L, last.close, stop, last.close + p.target_r * risk, last.ts,
                              f"bear trap below {L:.0f} (low {lo:.0f}, closed back above)")
        return None
    if kind == PULLBACK:
        if sideways(bars, lv, p)[0] or n < p.or_bars + 4:
            return None
        prev = bars[-2]
        for j in range(n - 3, max(p.or_bars, n - 1 - p.pb_look) - 1, -1):     # most recent confirmed breakout first
            hit = _breakout_at(bars, j, levels_upto(j), p)
            if hit is None:
                continue
            d, L = hit
            if d * (bars[j + 1].close - L) <= 0:
                continue                                 # not confirmed by the next bar
            after = bars[j + 1:-1]
            stop_lvl = L * (1 - d * p.stop_buffer_pct)
            if any(d * (b.close - stop_lvl) < 0 for b in after):
                return None                              # closed back through the level: failed, not a pullback
            touched = min(b.low for b in after) if d > 0 else max(b.high for b in after)
            if d * (touched - L) > p.pb_tol * L:
                return None                              # never came back to the level: no pullback yet
            rng = last.high - last.low
            strong = rng > 0 and d * (last.close - last.open) >= p.min_body * rng
            if not (strong and d * (last.close - (prev.high if d > 0 else prev.low)) > 0):
                return None
            stop = min(touched, stop_lvl) * (1 - p.buffer_pct) if d > 0 else max(touched, stop_lvl) * (1 + p.buffer_pct)
            risk = abs(last.close - stop)
            if risk <= 0:
                return None
            beyond = [x for x in levels if d * (x - last.close) > 0]
            nxt = min(beyond, key=lambda x: abs(x - last.close)) if beyond else None
            if nxt is not None and abs(nxt - last.close) < p.room_r * risk:
                return None
            return Signal(PULLBACK, d, L, last.close, stop, last.close + d * p.target_r * risk, last.ts,
                          f"{'up' if d > 0 else 'down'} pullback to {L:.0f} held")
        return None
    if kind == RANGE:
        if _bar_end(last) < p.range_after:
            return None
        er = efficiency([b.close for b in bars])
        hi, lo = max(b.high for b in bars), min(b.low for b in bars)
        if er > p.range_er_max or (lv.adr > 0 and hi - lo > p.range_adr_max * lv.adr):
            return None
        if not (lv.orl <= last.close <= lv.orh):
            return None
        return Signal(RANGE, 0, (hi + lo) / 2, last.close, last.close, last.close, last.ts,
                      f"range day (ER {er:.2f}, {(hi - lo) / max(lv.adr, 1):.2f} x ADR)", hi, lo)
    raise ValueError(f"unknown signal kind {kind}")
