"""Multi-index engine: one feed per index (indices.INDICES), scanned every loop; the best candidate trade wins.

Per index (IndexFeed):
  * its option contracts and spot token from Kite's dumps (validated, models.py);
  * the tradable expiry: the nearest one after today (expiry day trades the next contract, like SENSEX); monthly-only
    indices trade only in the expiry week (<= indices.EXPIRY_WEEK_DAYS trading days left), the only time their
    options decay like weeklies (the backtest's finding);
  * an option-chain window (ATM +/- WINDOW strikes) streamed in FULL mode (bid / ask / OI / volume), re-centred when
    spot moves; one WebSocket carries all six indices (~150 tokens of Kite's 3,000 per connection);
  * 5-minute index candles fetched once per bar (6 history calls every 5 minutes, far below Kite's 3/s);
  * its own Playbook (the same decision code as SENSEX, with the index's backtested parameters) and day state.

MultiIndex.candidates() returns every trade that could be entered now, best first (the backtest edge of that index
and setup). The caller (main.Bot) sizes and places at most one, in its own thread. Nothing here places orders.
A trade is never proposed unless every leg has a fresh two-sided quote no wider than the index's max spread.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Optional

from execution import validate_plan_quotes
from indices import EXPIRY_WEEK_DAYS, INDICES, ITM_PCT, IndexSpec
from models import Candle as CandleRow
from models import parse_rows
from pb_indicators import Candle
from playbook import (Decision, Playbook, PlaybookParams, TrendState, build_condor, build_credit, build_single,
                      trading_days_left, trend_state)
from strategy_engine import Chain, OptionQuote, TradePlan

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    index: str
    setup: str                    # single | condor | credit
    plan: TradePlan
    decision: Decision
    edge: float                   # backtest average net per trade of this index + setup (ranking key)
    spot: float


class IndexFeed:
    WINDOW = 12                   # strikes each side of ATM streamed in FULL mode
    RECENTRE_STEPS = 4            # re-centre the window when ATM has moved this many strikes
    HIST_DAYS = 7
    QUOTE_MAX_AGE_S = 15.0
    SPOT_MAX_AGE_S = 30.0

    def __init__(self, spec: IndexSpec, client, base: PlaybookParams) -> None:
        self.spec, self.client = spec, client
        self.pb = Playbook(dataclasses.replace(base, **spec.params))
        self.contracts = []
        self.tokens: set[int] = set()
        self.spot_token = 0
        self.ready = False
        self._center: Optional[float] = None
        self._window_exp: Optional[dt.date] = None
        self._hist: list[Candle] = []
        self._hist_wall: Optional[dt.datetime] = None
        self._next_hist = 0.0
        self._trend_key, self._trend = None, None

    # ------------------------------------------------------------------ setup
    def load(self) -> None:
        self.contracts = self.client.index_contracts(self.spec.name, self.spec.exchange)
        if not self.contracts:
            raise RuntimeError(f"no {self.spec.name} option contracts in the {self.spec.exchange} dump")
        self.tokens = {c.token for c in self.contracts}
        self.spot_token = self.client.index_token(self.spec.spot_exchange, self.spec.spot_symbol)
        self.client.subscribe_tokens({self.spot_token: self.spec.key})
        self._center, self._window_exp, self._next_hist = None, None, 0.0
        self.ready = True

    def expiry(self, today: dt.date) -> Optional[dt.date]:
        """The contract this index trades today, or None (monthly-only index outside its expiry week)."""
        later = sorted({c.expiry for c in self.contracts if c.expiry > today})
        if not later:
            return None
        e = later[0]
        if self.spec.monthly_only and trading_days_left(today, e) > EXPIRY_WEEK_DAYS:
            return None
        return e

    @property
    def active(self) -> bool:
        return self.ready and bool(self.spec.setups)

    def spot(self) -> Optional[float]:
        return self.client.tick_ltp(self.spot_token, self.SPOT_MAX_AGE_S) if self.spot_token else None

    # ---------------------------------------------------------------- refresh
    def refresh(self, now: dt.datetime) -> None:
        if not self.active:
            return
        exp = self.expiry(now.date())
        s = self.spot()
        if exp is None:
            return                                       # monthly index outside its expiry week: nothing to fetch
        if s:
            self._recentre(s, exp)
        if time.monotonic() >= self._next_hist:
            self._fetch_history(now)

    def _recentre(self, spot: float, exp: dt.date) -> None:
        step = self.spec.step
        atm = round(spot / step) * step
        near = self._center is not None and abs(atm - self._center) < self.RECENTRE_STEPS * step
        if self._window_exp == exp and near:
            return
        sel = [c for c in self.contracts if c.expiry == exp and abs(c.strike - atm) <= self.WINDOW * step]
        self.client.subscribe_full({c.token: c.key for c in sel})          # idempotent; the window only grows
        self._center, self._window_exp = atm, exp

    def _fetch_history(self, now: dt.datetime) -> None:
        try:
            raw = self.client.historical(self.spot_token, now - dt.timedelta(days=self.HIST_DAYS), now, "5minute")
            rows = parse_rows(CandleRow, raw or [], f"{self.spec.name} 5-minute candles")
            self._hist = [Candle(r.date, r.open, r.high, r.low, r.close) for r in rows]
            self._hist_wall = now
            # next fetch a few seconds after the next 5-minute boundary
            secs = (now.minute % 5) * 60 + now.second
            self._next_hist = time.monotonic() + (300 - secs) + 8
        except Exception:  # noqa: BLE001 - no history means no signal for this index, never a crash
            log.warning("%s history fetch failed; retrying in 30 s", self.spec.name, exc_info=True)
            self._next_hist = time.monotonic() + 30

    # ------------------------------------------------------------- read side
    def trend(self, now: dt.datetime) -> Optional[TrendState]:
        if not self._hist or self._hist_wall is None:
            return None
        key = (self._hist_wall, len(self._hist))
        if key != self._trend_key:
            try:
                self._trend = trend_state(self._hist, min(now, self._hist_wall))
            except Exception:  # noqa: BLE001
                log.exception("%s trend state failed", self.spec.name)
                self._trend = None
            self._trend_key = key
        return self._trend

    def chain(self, now: dt.datetime) -> Optional[Chain]:
        exp = self.expiry(now.date())
        s = self.spot()
        if exp is None or not s:
            return None
        flow = self.client.flow_snapshot(self.QUOTE_MAX_AGE_S)
        step = self.spec.step
        atm = round(s / step) * step
        quotes = {}
        for c in self.contracts:
            if c.expiry != exp or abs(c.strike - atm) > self.WINDOW * step:
                continue
            f = flow.get(c.token)
            if not f or not (f.get("ltp") or 0) > 0:
                continue
            quotes[(c.strike, c.kind)] = OptionQuote(c, float(f["ltp"]), float(f.get("oi") or 0), None, None,
                                                     float(f.get("volume") or 0), f.get("bid"), f.get("ask"))
        return Chain(exp, s, atm, step, 0.0, quotes) if quotes else None

    def bid_ask(self, token: int) -> Optional[tuple[float, float]]:
        f = self.client.flow_snapshot(10.0).get(token)
        if f and f.get("bid") and f.get("ask") and f["ask"] >= f["bid"] > 0:
            return float(f["bid"]), float(f["ask"])
        return None


def two_sided(plan: TradePlan, chain: Chain, max_spread_pct: float) -> Optional[str]:
    """None if every leg has a positive bid AND ask no wider than max_spread_pct of mid, else why not."""
    for leg in plan.legs:
        q = chain.get(leg.contract.strike, leg.contract.kind)
        if q is None or not (q.bid and q.ask and q.ask >= q.bid > 0):
            return f"{leg.contract.symbol} has no two-sided quote"
        mid = (q.bid + q.ask) / 2
        if (q.ask - q.bid) / mid * 100 > max_spread_pct:
            return f"{leg.contract.symbol} spread {(q.ask - q.bid) / mid * 100:.1f}% > {max_spread_pct:.1f}%"
    return None


class MultiIndex:
    def __init__(self, client, base: PlaybookParams, specs: Optional[dict] = None) -> None:
        self.specs = specs or INDICES
        self.feeds = {name: IndexFeed(spec, client, base) for name, spec in self.specs.items()}
        self.why_not: dict[str, str] = {}

    def load(self) -> list[str]:
        """Load every index; one that fails is skipped for the session (logged). Returns the loaded names."""
        ok = []
        for name, f in self.feeds.items():
            try:
                f.load()
                ok.append(name)
            except Exception:  # noqa: BLE001
                f.ready = False
                log.exception("Index %s unavailable this session", name)
        return ok

    def refresh(self, now: dt.datetime) -> None:
        for f in self.feeds.values():
            try:
                f.refresh(now)
            except Exception:  # noqa: BLE001
                log.exception("Index %s refresh failed", f.spec.name)

    def feed(self, name: str) -> Optional[IndexFeed]:
        return self.feeds.get(name)

    def bid_ask(self, contract) -> Optional[tuple[float, float]]:
        for f in self.feeds.values():
            if contract.token in f.tokens:                 # by token: a "NIFTY" prefix would also match NIFTYNXT50
                return f.bid_ask(contract.token)
        return None

    def candidates(self, now: dt.datetime, vix: float) -> list[Candidate]:
        """Every placeable trade right now, best edge first. Read-only: the caller marks the playbook decisions
        (took_single / skipped_signal / took_range) for what it actually does."""
        out: list[Candidate] = []
        for name, f in self.feeds.items():
            if not f.active:
                self.why_not[name] = "no backtested setup" if f.ready else "not loaded"
                continue
            spec = f.spec
            chain = f.chain(now)
            if chain is None:
                self.why_not[name] = ("outside its expiry week" if spec.monthly_only and f.expiry(now.date()) is None
                                      else "no live chain yet")
                continue
            ts = f.trend(now)
            itm = None if spec.itm_by_steps else ITM_PCT
            plan, setup, d = None, None, None
            if "single" in spec.setups:
                d = f.pb.single_signal(ts, now, vix)
                if d.action == "single":
                    plan, setup = build_single(chain, d.direction, now, d.why, itm_pct=itm), "single"
            if plan is None and (d is None or d.action != "single"):
                d = f.pb.range_signal(ts, now, vix)
                if d.action == "condor" and "condor" in spec.setups:
                    plan, setup = build_condor(chain, d.short_pct, d.wing_pct, d.why), "condor"
                elif d.action == "credit" and "credit" in spec.setups:
                    plan, setup = build_credit(chain, d.direction, d.short_pct, d.wing_pct, d.why), "credit"
            if plan is None or setup is None:
                self.why_not[name] = "no signal" if d is None or d.action == "none" else "strikes not quoted yet"
                continue
            why = two_sided(plan, chain, spec.max_spread_pct)
            if why is None:
                qc = validate_plan_quotes(plan, chain, max_spread_pct=spec.max_spread_pct)
                why = None if qc.ok else qc.reason
            if why:
                self.why_not[name] = f"{setup}: {why}"
                continue
            plan = dataclasses.replace(plan, underlying=name,
                                       rationale=plan.rationale.replace("[PLAYBOOK]", f"[PLAYBOOK {name}]", 1))
            self.why_not[name] = f"{setup} ready"
            out.append(Candidate(name, setup, plan, d, float(spec.edge.get(setup, 0.0)), chain.spot))
        out.sort(key=lambda c: c.edge, reverse=True)
        return out

    # ------------------------------------------------------------- persistence
    def export(self) -> dict:
        return {name: f.pb.day.export() for name, f in self.feeds.items()}

    def restore(self, d: dict) -> None:
        from playbook import DayState
        for name, st in (d or {}).items():
            if name in self.feeds:
                self.feeds[name].pb.day = DayState.restore(st or {})
