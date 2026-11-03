"""A TradingEngine on a FakeMarket with a deterministic paper broker, plus a candidate injector."""
from __future__ import annotations

import dataclasses
import os
import random

from tests.helpers import SETTINGS, FakeClock, FakeMarket, contract, day, session, tmp_store, trend_days

from costs import CostParams
from sensexpro.engine import NullNotifier, TradingEngine
from sensexpro.execution import PaperBroker
from sensexpro.models import Candidate
from sensexpro.strategies.ensemble import Ensemble

TODAY = day(3)
EXPIRY = day(5)
NO_RANDOM = dataclasses.replace(SETTINGS.execution.paper, reject_prob=0.0, no_fill_prob=0.0, partial_fill_prob=0.0)


class Inject:
    """A strategy that emits whatever the test queued for the next bar."""

    def __init__(self, sid: str = "orb") -> None:
        self.id = sid
        self.queue: list = []

    def evaluate(self, f, i):
        out, self.queue = self.queue, []
        return out


def bars_until(n_today: int, per_bar: float = 10.0):
    base = trend_days(3, per_bar=per_bar, wiggle=5)
    closes = [base[-1].close + per_bar * k for k in range(1, n_today + 1)]
    return base + session(TODAY, closes, start_open=base[-1].close, rng=15)


class Kit:
    def __init__(self, store=None, tmpdir=None, mode="PAPER", sim=NO_RANDOM, broker=None, positions_fn=None,
                 lots=1, capital=200_000.0):
        if store is None:
            store, tmpdir = tmp_store()
        self.store, self.dir = store, tmpdir
        self.clock = FakeClock(1000.0)
        self.bars = bars_until(15)                                  # last bar ends 10:30
        spot = self.bars[-1].close
        atm = round(spot / 100) * 100
        self.contracts = [contract(k, kind, EXPIRY) for k in range(int(atm) - 500, int(atm) + 600, 100)
                          for kind in ("CE", "PE")]
        self.market = FakeMarket("SENSEX", self.clock, self.contracts, TODAY)
        self.market.spot_px = spot
        self.notifier = NullNotifier()
        self.broker = broker or PaperBroker(sim, self.market.quote, SETTINGS.data.quote_stale_s,
                                            rng=random.Random(7), now_mono=self.clock)
        self.engine = TradingEngine(SETTINGS, mode, store, {"SENSEX": self.market}, self.broker, CostParams(),
                                    self.notifier, lots_fn=lambda ix: lots, max_lots=10,
                                    broker_positions_fn=positions_fn, mono=self.clock,
                                    state_file=os.path.join(tmpdir, "state.json"))
        self.inject = Inject("orb")
        self.engine.ens = Ensemble(SETTINGS.strategy, [self.inject])
        self.quote_all(200.0, 201.0)
        self.engine.start_session(capital, TODAY)

    @property
    def atm(self):
        return round(self.market.spot_px / 100) * 100

    def quote_all(self, bid, ask, **kw):
        for c in self.contracts:
            self.market.set_quote(c, bid, ask, **kw)

    def candidate(self, direction="CE", key="setup-1", strength=1.0, inval=None):
        b = self.bars[-1]
        return Candidate("SENSEX", "orb", direction, b.end, key, strength, b.close,
                         inval if inval is not None else b.low - 60, ("test signal",), {}, False)

    def bar(self, closes=None, push_candidate=None):
        """Append completed bars, push the frame, optionally queue a candidate for this bar."""
        if closes:
            last = self.bars[-1]
            k0 = (last.start.hour * 60 + last.start.minute - 555) // 5 + 1
            prev = last.close
            from tests.helpers import bar5
            for n, c in enumerate(closes):
                self.bars.append(bar5(TODAY, k0 + n, prev, c, max(prev, c) + 15, min(prev, c) - 15))
                prev = c
            self.market.spot_px = self.bars[-1].close
        if push_candidate is not None:                       # stamped with the bar it arrives on
            b = self.bars[-1]
            self.inject.queue.append(dataclasses.replace(push_candidate, bar_end=b.end, ref_price=b.close))
        self.market.push_frame(self.bars)

    def now(self, plus_s: int = 5):
        b = self.bars[-1]
        return b.end + __import__("datetime").timedelta(seconds=plus_s)

    def step(self, plus_s: int = 5, advance: float = 1.0):
        self.clock.advance(advance)
        self.quote_all(*self._last_px, **self._last_kw)
        self.engine.step(self.now(plus_s))

    _last_px = (200.0, 201.0)
    _last_kw: dict = {}

    def set_px(self, bid, ask, **kw):
        self._last_px, self._last_kw = (bid, ask), kw
        self.quote_all(bid, ask, **kw)
