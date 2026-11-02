"""Broker-call pacing: token buckets per Kite endpoint class, and a circuit breaker for data calls.

Kite Connect limits (published): quote / LTP / OHLC 1 request/s; historical candles 3/s; order placement 10/s (and
per-minute / per-day caps); everything else 10/s. Each class gets its own bucket so a burst of history fetches can
never starve a quote, and a quote can never delay an order.

The breaker protects DATA calls only. After `threshold` consecutive network failures it opens for `cooldown_s` and
data calls fail fast (no hammering a dead link, no 429 storms); then one probe is let through (half-open) and success
closes it. Order and position calls never pass through it: an exit must always be attempted.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional


class TokenBucket:
    """`rate` tokens per second, at most `burst` stored. Thread-safe; `clock` and `sleep` are injectable for tests."""

    def __init__(self, rate: float, burst: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate, self.burst = float(rate), float(burst)
        self._clock, self._sleep = clock, sleep
        self._tokens, self._last = float(burst), clock()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> float:
        """Take one token, waiting if needed. Returns the seconds waited."""
        with self._lock:                                   # one waiter at a time keeps the order fair (FIFO-ish)
            now = self._clock()
            self._refill(now)
            wait = 0.0
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                self._sleep(wait)
                self._refill(self._clock())
            self._tokens -= 1.0
            return wait

    # backwards-compatible name used by the old fixed-gap limiter
    wait = acquire


KITE_LIMITS = {          # endpoint class -> (requests per second, burst)
    "quote": (1.0, 1.0),
    "historical": (3.0, 3.0),
    "order": (8.0, 8.0),             # 10/s published; stay below
    "default": (8.0, 8.0),
}

_ENDPOINT_OF = {
    "quote": "quote", "ltp": "quote", "ohlc": "quote",
    "historical_data": "historical",
    "place_order": "order", "modify_order": "order", "cancel_order": "order",
}


def endpoint_for(fn: Callable) -> str:
    return _ENDPOINT_OF.get(getattr(fn, "__name__", ""), "default")


class CircuitOpen(ConnectionError):
    """Raised instead of calling the broker while the breaker is open (a ConnectionError, so callers that already
    handle network failures handle this too)."""


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0, max_cooldown_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic, jitter: Callable[[], float] = random.random) -> None:
        self.threshold, self.base, self.max = threshold, cooldown_s, max_cooldown_s
        self._clock, self._jitter = clock, jitter
        self.state = self.CLOSED
        self.failures = 0
        self._trips = 0
        self._open_until = 0.0
        self._probe_out = False
        self._lock = threading.Lock()

    def before(self) -> None:
        with self._lock:
            if self.state == self.OPEN:
                if self._clock() < self._open_until:
                    raise CircuitOpen(f"broker data circuit open for {self._open_until - self._clock():.0f}s more")
                self.state, self._probe_out = self.HALF_OPEN, False
            if self.state == self.HALF_OPEN:
                if self._probe_out:
                    raise CircuitOpen("broker data circuit half-open: probe in flight")
                self._probe_out = True

    def success(self) -> None:
        with self._lock:
            self.state, self.failures, self._trips, self._probe_out = self.CLOSED, 0, 0, False

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == self.HALF_OPEN or self.failures >= self.threshold:
                self._trips += 1
                cool = min(self.base * 2 ** (self._trips - 1), self.max) * (1.0 + 0.2 * self._jitter())
                self.state, self._open_until, self._probe_out = self.OPEN, self._clock() + cool, False


def backoff_s(attempt: int, base: float = 1.0, cap: float = 20.0,
              jitter: Optional[Callable[[], float]] = None) -> float:
    """Exponential backoff with up to 25% jitter: 1, 2, 4, 8, 16, 20, ... seconds."""
    j = (jitter or random.random)()
    return min(base * 2 ** attempt, cap) * (1.0 + 0.25 * j)
