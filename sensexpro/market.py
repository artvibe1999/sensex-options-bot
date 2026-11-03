"""Per-index market data (brief section 3): instrument master, 1-minute candles, option quotes.

IndexMarket, for one index:
  * contracts / spot / future tokens from Kite's instrument dump of the day (never hardcoded; lot size, tick size,
    expiry, strike come from it);
  * 1-minute candles of the index (Kite historical, exchange-built) and of its nearest future (volume), fetched a few
    seconds after each minute closes; only closed candles are kept; validated (duplicates, disorder, bad OHLC,
    session) and persisted; aggregated to 5 and 15 minutes; gaps flagged;
  * an option window (ATM +/- N strikes of the selected expiry) streamed in FULL mode: bid / ask / depth quantities /
    volume / OI / day VWAP, with the exchange timestamp and the local receive time; re-centred as spot moves.
Nothing here places orders.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Callable, Optional

from indices import IndexSpec

from . import candles as cd
from .features import Frame
from .models import IST, Bar, ContractMeta, OptionQuote
from .options import choose_expiry
from .settings import Settings

log = logging.getLogger(__name__)


def _aware(t) -> Optional[dt.datetime]:
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return dt.datetime.fromtimestamp(t, IST)
    if isinstance(t, dt.datetime):
        return t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
    return None


class IndexMarket:
    RECENTRE_STEPS = 2

    def __init__(self, spec: IndexSpec, client, s: Settings, store=None,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.spec, self.client, self.s, self.store, self.mono = spec, client, s, store, mono
        self.contracts: list[ContractMeta] = []
        self.step = spec.step
        self.by_token: dict[int, ContractMeta] = {}
        self.spot_token = 0
        self.future_token: Optional[int] = None
        self.instruments_day: Optional[dt.date] = None
        self.ready = False
        self.bars1: list[Bar] = []
        self.bars5: list[Bar] = []
        self.bars15: list[Bar] = []
        self.frame: Optional[Frame] = None
        self.new_bar = False                    # a new completed 5-minute bar since the last consume_new_bar()
        self.data_ok = False
        self.data_why = "not loaded"
        self._next_fetch = 0.0
        self._loaded_from: Optional[dt.datetime] = None
        self._center: Optional[float] = None
        self._window_exp: Optional[dt.date] = None
        self._last_record = 0.0

    # ------------------------------------------------------------------ setup
    def load(self, today: dt.date) -> None:
        raw = self.client.index_contracts(self.spec.name, self.spec.exchange)
        self.contracts = [ContractMeta(self.spec.name, c.exchange, c.token, c.symbol, c.expiry, float(c.strike), c.kind,
                                       int(c.lot_size), float(c.tick_size)) for c in raw]
        if not self.contracts:
            raise RuntimeError(f"no {self.spec.name} options in the {self.spec.exchange} instrument master")
        self.by_token = {c.token: c for c in self.contracts}
        self.step = self._dump_step(today)
        self.spot_token = self.client.index_token(self.spec.spot_exchange, self.spec.spot_symbol)
        self.client.subscribe_tokens({self.spot_token: self.spec.key})
        try:
            self.future_token = self.client.index_future(self.spec.name, self.spec.exchange, today)
        except Exception:  # noqa: BLE001 - volume becomes unavailable; strategies that need it will not fire
            log.exception("%s: future lookup failed", self.spec.name)
            self.future_token = None
        if self.future_token is None:
            log.warning("%s: no index future found: volume unavailable (volume-dependent rules will not fire)",
                        self.spec.name)
        self.instruments_day = today
        self._center = self._window_exp = None
        self._next_fetch = 0.0
        self._loaded_from = None
        self.ready = True

    def _dump_step(self, today: dt.date) -> float:
        """Strike interval as listed in the instrument master (most common gap of the nearest expiry)."""
        exp = choose_expiry(self.contracts, today, False)
        ks = sorted({c.strike for c in self.contracts if c.expiry == exp})
        gaps = [round(b - a, 2) for a, b in zip(ks, ks[1:]) if b > a]
        if not gaps:
            return self.spec.step
        step = max(set(gaps), key=gaps.count)
        if step != self.spec.step:
            log.warning("%s: instrument master strike interval %.2f differs from the configured %.2f: using %.2f",
                        self.spec.name, step, self.spec.step, step)
        return step

    # ------------------------------------------------------------------ live data
    def spot(self) -> Optional[float]:
        return self.client.tick_ltp(self.spot_token, self.s.data.spot_stale_s) if self.spot_token else None

    def expiry(self, today: dt.date) -> Optional[dt.date]:
        return choose_expiry(self.contracts, today, self.s.strategy.contracts.avoid_expiry_day_contract)

    def quote(self, token: int) -> Optional[OptionQuote]:
        c = self.by_token.get(token)
        f = self.client.flow_snapshot(3600.0).get(token)
        if c is None or not f:
            return None
        return OptionQuote(c, f.get("bid"), f.get("ask"), f.get("bid_qty"), f.get("ask_qty"), f.get("ltp"),
                           f.get("volume"), f.get("oi"), f.get("avg"), _aware(f.get("ex_ts")), float(f["mono"]),
                           dt.datetime.fromtimestamp(f.get("wall") or time.time(), IST))

    def quotes(self) -> dict[int, OptionQuote]:
        out = {}
        for tok in list(self.client.flow_snapshot(3600.0).keys()):
            q = self.quote(tok)
            if q is not None:
                out[tok] = q
        return out

    def rest_quote(self, token: int) -> Optional[OptionQuote]:
        """REST fallback for a held contract whose stream went quiet (rate-limited by the client)."""
        c = self.by_token.get(token)
        if c is None:
            return None
        d = (self.client.quote([c.key]) or {}).get(c.key) or {}
        depth = d.get("depth") or {}
        buy, sell = depth.get("buy") or [], depth.get("sell") or []
        return OptionQuote(c, buy[0].get("price") if buy else None, sell[0].get("price") if sell else None,
                           buy[0].get("quantity") if buy else None, sell[0].get("quantity") if sell else None,
                           d.get("last_price"), d.get("volume"), d.get("oi"), d.get("average_price"),
                           _aware(d.get("timestamp")), self.mono(), dt.datetime.now(IST))

    def liquidity_ok(self, today: dt.date) -> bool:
        """At least one ATM option of the tradable expiry has a fresh, two-sided quote no wider than 2x the entry
        spread limit (else the regime is LOW_LIQUIDITY_OR_UNCERTAIN)."""
        exp, s = self.expiry(today), self.spot()
        if exp is None or not s:
            return False
        atm = round(s / self.step) * self.step
        lim = 2 * self.s.strategy.contracts.max_spread_pct
        for c in self.contracts:
            if c.expiry == exp and c.strike == atm:
                q = self.quote(c.token)
                if q and not q.problem(self.mono(), self.s.data.quote_stale_s) and (q.spread_pct or 99) <= lim:
                    return True
        return False

    # ------------------------------------------------------------------ refresh
    def refresh(self, now: dt.datetime) -> None:
        if not self.ready:
            return
        s = self.spot()
        exp = self.expiry(now.date())
        if s and exp:
            self._recentre(s, exp)
        if self.mono() >= self._next_fetch:
            self._fetch(now)
        self._record(now)

    def _recentre(self, spot: float, exp: dt.date) -> None:
        step = self.step
        atm = round(spot / step) * step
        if self._window_exp == exp and self._center is not None and abs(atm - self._center) < self.RECENTRE_STEPS * step:
            return
        n = self.s.data.option_window_strikes
        sel = [c for c in self.contracts if c.expiry == exp and abs(c.strike - atm) <= n * step]
        self.client.subscribe_full({c.token: c.key for c in sel})
        self._center, self._window_exp = atm, exp

    def _fetch(self, now: dt.datetime) -> None:
        delay = self.s.data.candle_fetch_delay_s
        nxt = (60 - now.second) + delay
        try:
            start = now - dt.timedelta(days=self.s.data.warmup_days) if self._loaded_from is None else \
                now.replace(hour=9, minute=15, second=0, microsecond=0)
            raw = self.client.historical(self.spot_token, start, now, "minute") or []
            bars, problems = cd.parse_rows(raw)
            for p in problems[:3]:
                log.warning("%s candles: %s", self.spec.name, p)
            if self.future_token:
                fraw = self.client.historical(self.future_token, start, now, "minute") or []
                fbars, _ = cd.parse_rows(fraw)
                bars = cd.with_volume(bars, cd.completed(fbars, now))
            bars = cd.completed(bars, now)
            self._merge(bars)
            self._loaded_from = self._loaded_from or start
            self._next_fetch = self.mono() + nxt
        except Exception:  # noqa: BLE001 - no candles = no signals (DATA_UNSAFE), never a crash
            log.warning("%s: candle fetch failed; retrying in 20 s", self.spec.name, exc_info=True)
            self._next_fetch = self.mono() + 20
        self._check(now)

    def _merge(self, bars: list[Bar]) -> None:
        if not bars:
            return
        have = {b.start: n for n, b in enumerate(self.bars1)}
        new = []
        for b in bars:
            if b.start in have:
                self.bars1[have[b.start]] = b             # revised (e.g. late volume) candle replaces the old one
            else:
                new.append(b)
        self.bars1 = sorted(self.bars1 + new, key=lambda b: b.start)
        cutoff = self.bars1[-1].start - dt.timedelta(days=self.s.data.warmup_days + 3)
        self.bars1 = [b for b in self.bars1 if b.start >= cutoff]
        if self.store is not None and new:
            try:
                self.store.save_candles(self.spec.name, new)
            except Exception:  # noqa: BLE001
                log.exception("Candle persistence failed")
        b5 = cd.aggregate(self.bars1, 5)
        last_old = self.bars5[-1].start if self.bars5 else None
        self.bars5, self.bars15 = b5, cd.aggregate(self.bars1, 15)
        if self.bars5 and self.bars5[-1].start != last_old:
            self.frame = Frame.build(self.spec.name, self.bars5, self.bars15, self.s.strategy.indicators)
            self.new_bar = True

    def _check(self, now: dt.datetime) -> None:
        """data_ok: candles current (the last closed minute is present) and no gap in the last 30 minutes."""
        if not self.bars1:
            self.data_ok, self.data_why = False, "no candles yet"
            return
        sess_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now < sess_open + dt.timedelta(minutes=2):
            self.data_ok, self.data_why = True, ""
            return
        last_closed = min(now, now.replace(hour=15, minute=30, second=0, microsecond=0)) - dt.timedelta(minutes=1)
        lag = (last_closed - self.bars1[-1].start).total_seconds()
        if lag > 120:
            self.data_ok, self.data_why = False, f"last 1-minute candle {lag / 60:.0f} min old"
            return
        miss = cd.missing_minutes(self.bars1, max(sess_open, now - dt.timedelta(minutes=30)), self.bars1[-1].start)
        if len(miss) > self.s.data.max_gap_bars:
            self.data_ok, self.data_why = False, f"{len(miss)} missing 1-minute candle(s) in the last 30 min"
            return
        self.data_ok, self.data_why = True, ""

    def consume_new_bar(self) -> bool:
        nb, self.new_bar = self.new_bar, False
        return nb

    def _record(self, now: dt.datetime) -> None:
        """Option quote snapshots for research (indices in data.record_quotes_for), every record_quotes_every_s."""
        if self.store is None or self.spec.name not in self.s.data.record_quotes_for:
            return
        if self.mono() - self._last_record < self.s.data.record_quotes_every_s:
            return
        self._last_record = self.mono()
        try:
            ts = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
            rows = []
            for tok, q in self.quotes().items():
                if self.mono() - q.recv_mono > 5:
                    continue
                rows.append((ts, q.contract.symbol, q.bid, q.ask, q.bid_qty, q.ask_qty, q.ltp, q.volume, q.oi,
                             q.avg_price, q.exchange_ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
                             if q.exchange_ts else None))
            self.store.quotes(rows)
        except Exception:  # noqa: BLE001 - recording must never disturb trading
            log.exception("Quote recording failed")
