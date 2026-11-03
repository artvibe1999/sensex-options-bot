"""Deterministic fixtures: synthetic candles, quotes, a fake market, a fake clock. No live market data."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sensexpro import candles as cd  # noqa: E402
from sensexpro.features import Frame  # noqa: E402
from sensexpro.models import IST, Bar, ContractMeta, OptionQuote  # noqa: E402
from sensexpro.settings import load_settings  # noqa: E402

D0 = dt.date(2026, 10, 26)          # a Monday
SETTINGS = load_settings(env={})


def day(n: int) -> dt.date:
    """n-th trading day (Mon-Fri) from D0."""
    d, k = D0, 0
    while k < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            k += 1
    return d


def t(d: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def bar5(d: dt.date, k: int, o: float, c: float, h: Optional[float] = None, lo: Optional[float] = None,
         v: Optional[float] = 1000.0) -> Bar:
    start = t(d, 9, 15) + dt.timedelta(minutes=5 * k)
    h = max(o, c) + 10 if h is None else h
    lo = min(o, c) - 10 if lo is None else lo
    return Bar(start, 5, o, h, lo, c, v)


def session(d: dt.date, closes: list, start_open: Optional[float] = None, rng: float = 30.0,
            vols: Optional[list] = None) -> list[Bar]:
    """5-minute bars from a list of closes (open = previous close)."""
    out, prev = [], start_open if start_open is not None else closes[0]
    for k, c in enumerate(closes):
        v = vols[k] if vols else 1000.0
        out.append(bar5(d, k, prev, c, max(prev, c) + rng, min(prev, c) - rng, v))
        prev = c
    return out


def trend_days(n_days: int, start: float = 50000.0, per_bar: float = 4.0, rng: float = 30.0,
               wiggle: float = 25.0) -> list[Bar]:
    """n_days full sessions (75 bars) of a steady trend with alternating wiggles (warm-up history)."""
    out, px = [], start
    for n in range(n_days):
        closes = []
        for k in range(75):
            px += per_bar
            closes.append(px + (wiggle if k % 2 else -wiggle))
        out += session(day(n), closes, rng=rng)
    return out


def agg15(bars5: list[Bar]) -> list[Bar]:
    """15-minute bars from 5-minute bars (complete triples only, aligned to 09:15)."""
    out, by_day = [], {}
    for b in bars5:
        by_day.setdefault(b.start.date(), []).append(b)
    for d, bs in by_day.items():
        for i in range(0, len(bs) - 2, 3):
            g = bs[i:i + 3]
            if g[0].start.minute % 15 != 0 or (g[0].start.hour * 60 + g[0].start.minute - 555) % 15:
                continue
            vols = [x.volume for x in g]
            out.append(Bar(g[0].start, 15, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                           None if None in vols else sum(vols)))
    return out


def frame(bars5: list[Bar], index: str = "SENSEX") -> Frame:
    return Frame.build(index, bars5, agg15(bars5), SETTINGS.strategy.indicators)


def contract(strike: float, kind: str = "CE", expiry: dt.date = dt.date(2026, 11, 5), lot: int = 20,
             token: Optional[int] = None, index: str = "SENSEX", exchange: str = "BFO") -> ContractMeta:
    tok = token or int(strike) * 10 + (1 if kind == "CE" else 2)
    return ContractMeta(index, exchange, tok, f"{index}26N{int(strike)}{kind}", expiry, strike, kind, lot, 0.05)


def quote(c: ContractMeta, bid: float, ask: float, mono: float = 100.0, bid_qty: int = 2000, ask_qty: int = 2000,
          volume: float = 1e6, avg: Optional[float] = None, ex_ts: Optional[dt.datetime] = None) -> OptionQuote:
    return OptionQuote(c, bid, ask, bid_qty, ask_qty, (bid + ask) / 2, volume, 1e5, avg, ex_ts, mono,
                       ex_ts or dt.datetime(2026, 11, 3, 10, 0, tzinfo=IST))


class FakeClock:
    def __init__(self, mono: float = 100.0) -> None:
        self.t = mono

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeSpec:
    def __init__(self, name: str = "SENSEX") -> None:
        self.name, self.step, self.exchange = name, 100.0, "BFO"


class FakeMarket:
    """Implements the IndexMarket surface the engine uses, driven directly by the test."""

    def __init__(self, name: str, clock: FakeClock, contracts: list, today: dt.date) -> None:
        self.spec = FakeSpec(name)
        self.step = 100.0
        self.clock = clock
        self.contracts = contracts
        self.by_token = {c.token: c for c in contracts}
        self._quotes: dict[int, OptionQuote] = {}
        self.instruments_day = today
        self.frame: Optional[Frame] = None
        self.new_bar = False
        self.data_ok, self.data_why = True, ""
        self.future_token = 1
        self.spot_px: Optional[float] = None
        self.liq = True
        self.rest_calls = 0

    def refresh(self, now) -> None:
        pass

    def spot(self):
        return self.spot_px

    def set_quote(self, c: ContractMeta, bid: float, ask: float, **kw) -> None:
        self._quotes[c.token] = quote(c, bid, ask, mono=self.clock(), **kw)

    def quote(self, token: int):
        return self._quotes.get(token)

    def quotes(self):
        return dict(self._quotes)

    def rest_quote(self, token: int):
        self.rest_calls += 1
        return None

    def liquidity_ok(self, today) -> bool:
        return self.liq

    def consume_new_bar(self) -> bool:
        nb, self.new_bar = self.new_bar, False
        return nb

    def push_frame(self, bars5: list[Bar]) -> None:
        self.frame = frame(bars5, self.spec.name)
        self.new_bar = True


def tmp_store():
    from sensexpro.storage import Store
    d = tempfile.mkdtemp(prefix="sp-test-")
    return Store(os.path.join(d, "t.db")), d


def one_minute(d: dt.date, closes: list, vol: Optional[float] = 100.0) -> list[Bar]:
    out, prev = [], closes[0]
    for k, c in enumerate(closes):
        s = t(d, 9, 15) + dt.timedelta(minutes=k)
        out.append(Bar(s, 1, prev, max(prev, c) + 1, min(prev, c) - 1, c, vol))
        prev = c
    return out


__all__ = ["cd", "Bar", "IST", "SETTINGS", "day", "t", "bar5", "session", "trend_days", "agg15", "frame",
           "contract", "quote", "FakeClock", "FakeMarket", "tmp_store", "one_minute"]
