"""Strategy interface. A strategy reads a Frame and returns Candidates for its last completed bar. It has no access to
the broker, the risk manager or the execution engine: it cannot place orders (brief 1.1 rule 15)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional, Protocol

from ..features import Frame
from ..models import CE, PE, Candidate


class Strategy(Protocol):
    id: str

    def evaluate(self, f: Frame, i: int) -> list[Candidate]: ...


def close_position(f: Frame, i: int) -> float:
    """Where the close sits in the bar's range: 0 = low, 1 = high (0.5 for a zero-range bar)."""
    b = f.bars[i]
    return 0.5 if b.range <= 0 else (b.close - b.low) / b.range


def directional_strength(f: Frame, i: int, direction: str) -> float:
    """0..1 quality of the confirmation candle: body in the trade's direction and a close near the extreme."""
    b = f.bars[i]
    pos = close_position(f, i)
    body_ok = b.close > b.open if direction == CE else b.close < b.open
    s = pos if direction == CE else 1.0 - pos
    return round(s if body_ok else s * 0.5, 3)


def day_key(f: Frame, i: int) -> str:
    return f.bars[i].start.date().isoformat()


def ready(f: Frame, i: int, *series: list) -> bool:
    return all(s[i] is not None for s in series)


def macd_rising(f: Frame, i: int, n: int = 2) -> Optional[bool]:
    h = f.macd_hist
    if i < n or any(h[k] is None for k in range(i - n, i + 1)):
        return None
    return all(h[k] > h[k - 1] for k in range(i - n + 1, i + 1))


def macd_falling(f: Frame, i: int, n: int = 2) -> Optional[bool]:
    h = f.macd_hist
    if i < n or any(h[k] is None for k in range(i - n, i + 1)):
        return None
    return all(h[k] < h[k - 1] for k in range(i - n + 1, i + 1))


def sign(direction: str) -> int:
    return 1 if direction == CE else -1


def params(p: SimpleNamespace, name: str) -> SimpleNamespace:
    return getattr(p, name)


__all__ = ["Strategy", "CE", "PE", "Candidate", "close_position", "directional_strength", "day_key", "ready",
           "macd_rising", "macd_falling", "sign", "params"]
