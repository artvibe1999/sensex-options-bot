"""Candle validation and aggregation (1-minute -> 5 / 15 minutes) without look-ahead.

1-minute candles come from Kite's historical API (exchange-built OHLC) for the index, with volume from the index's
current-month future (an index has no traded volume of its own). Only candles that had CLOSED when they were fetched
are used: the last row of a fetch is still forming and is dropped.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional, Sequence

from .models import IST, Bar

log = logging.getLogger(__name__)

SESSION_START_MIN = 9 * 60 + 15          # 09:15 IST
SESSION_END_MIN = 15 * 60 + 30           # 15:30 IST (index candles)


def _aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)


def parse_rows(rows: Iterable[dict], minutes: int = 1) -> tuple[list[Bar], list[str]]:
    """Validated bars from Kite historical rows (date/open/high/low/close/volume). Returns (bars, problems).
    Drops malformed rows, duplicates, out-of-order rows and anything outside the regular session."""
    out: list[Bar] = []
    problems: list[str] = []
    last: Optional[dt.datetime] = None
    for r in rows:
        try:
            t = _aware(r["date"] if isinstance(r["date"], dt.datetime) else dt.datetime.fromisoformat(str(r["date"])))
            vol = r.get("volume")
            b = Bar(t, minutes, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                    float(vol) if vol not in (None, "") else None)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"bad row {r!r}: {exc}")
            continue
        mod = t.hour * 60 + t.minute
        if not SESSION_START_MIN <= mod < SESSION_END_MIN:
            continue
        if last is not None and t <= last:
            problems.append(f"{'duplicate' if t == last else 'out-of-order'} candle {t.isoformat()}")
            continue
        out.append(b)
        last = t
    return out, problems


def completed(bars: Sequence[Bar], fetched_at: dt.datetime) -> list[Bar]:
    """Bars whose end is not after the fetch time (the forming bar is dropped)."""
    return [b for b in bars if b.end <= fetched_at]


def with_volume(index_bars: Sequence[Bar], future_bars: Sequence[Bar]) -> list[Bar]:
    """Index OHLC with the future's volume for the same minute (None where the future has no candle)."""
    vol = {b.start: b.volume for b in future_bars}
    return [Bar(b.start, b.minutes, b.open, b.high, b.low, b.close, vol.get(b.start), b.gap) for b in index_bars]


def bucket_start(t: dt.datetime, minutes: int) -> dt.datetime:
    t = _aware(t)
    mod = t.hour * 60 + t.minute
    k = (mod - SESSION_START_MIN) // minutes
    start_min = SESSION_START_MIN + k * minutes
    return t.replace(hour=start_min // 60, minute=start_min % 60, second=0, microsecond=0)


def aggregate(bars_1m: Sequence[Bar], minutes: int) -> list[Bar]:
    """Aggregate 1-minute bars into `minutes` bars aligned to 09:15. A bucket is emitted only when it is complete: its
    last minute is present, or later bars exist (then it is flagged gap=True if minutes are missing). The session's
    final bucket is cut at 15:30."""
    if minutes == 1:
        return list(bars_1m)
    groups: dict[dt.datetime, list[Bar]] = {}
    order: list[dt.datetime] = []
    for b in bars_1m:
        k = bucket_start(b.start, minutes)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(b)
    out = []
    for n, k in enumerate(order):
        g = groups[k]
        end_min = min(k.hour * 60 + k.minute + minutes, SESSION_END_MIN)
        expected = end_min - (k.hour * 60 + k.minute)
        last_needed = k + dt.timedelta(minutes=expected - 1)
        has_last = g[-1].start >= last_needed
        later = n + 1 < len(order)
        if not has_last and not later:
            continue                                      # still forming
        vols = [b.volume for b in g]
        out.append(Bar(k, expected, g[0].open, max(b.high for b in g), min(b.low for b in g), g[-1].close,
                       None if any(v is None for v in vols) else float(sum(vols)),
                       gap=len(g) < expected or any(b.gap for b in g)))
    return out


def missing_minutes(bars_1m: Sequence[Bar], since: dt.datetime, until: dt.datetime) -> list[dt.datetime]:
    """1-minute session starts in [since, until) with no candle (feed gaps)."""
    have = {b.start for b in bars_1m}
    out = []
    t = _aware(since).replace(second=0, microsecond=0)
    until = _aware(until)
    while t < until:
        mod = t.hour * 60 + t.minute
        if SESSION_START_MIN <= mod < SESSION_END_MIN and t not in have:
            out.append(t)
        t += dt.timedelta(minutes=1)
    return out
