"""Kite Connect wrapper: auth, instruments, quotes, history, orders, WebSocket.

SENSEX options trade on the BFO segment. Instrument tokens, strikes, expiries
and lot sizes are read from the live instruments dump, never hardcoded,
because lot size, strike spacing and expiry weekday have all been revised by
the exchange/SEBI before.

Since 1 April 2026, placing orders through the API requires the calling IP to
be registered in the Kite developer console (market data / WebSocket do not).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Optional

import requests
from kiteconnect import KiteConnect, KiteTicker
from kiteconnect import exceptions as kex

from config import Credentials
from token_store import is_fresh, read_token

log = logging.getLogger(__name__)

_RETRYABLE = (kex.NetworkException, kex.GeneralException, requests.exceptions.RequestException)


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class OptionContract:
    token: int
    symbol: str
    exchange: str
    strike: float
    expiry: dt.date
    kind: str  # "CE" or "PE"
    lot_size: int
    tick_size: float

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"


class _RateLimiter:
    def __init__(self, per_second: float) -> None:
        self._gap = 1.0 / per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._gap - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class ZerodhaClient:
    def __init__(self, creds: Credentials, token_file: Optional[str] = None) -> None:
        self._creds = creds
        self._token_file = token_file
        self.kite = KiteConnect(api_key=creds.api_key)
        self._data_limit = _RateLimiter(3)    # Kite: ~3 req/s for data endpoints
        self._order_limit = _RateLimiter(8)   # Kite: 10 orders/s; stay below
        self._contracts: list[OptionContract] = []
        self.spot_token: int = 0
        self.vix_token: int = 0
        self._keys: dict[int, str] = {}                      # token -> "EXCH:SYMBOL"
        self._ticks: dict[int, tuple[float, float]] = {}     # token -> (ltp, monotonic ts)
        self._subs: set[int] = set()
        self._ticker: Optional[KiteTicker] = None
        self._ws_connected = threading.Event()
        self.stream_dead = False

    # ------------------------------------------------------------------ auth
    def authenticate(self) -> None:
        """Pick up today's access token and verify it.

        Order: token file written by token_service.py (if issued after the last
        06:00 IST expiry), then KITE_ACCESS_TOKEN, then a one-time request token.
        """
        c = self._creds
        candidates: list[str] = []
        rec = read_token(self._token_file) if self._token_file else None
        if rec and is_fresh(rec):
            candidates.append(rec["access_token"])
        if c.access_token and c.access_token not in candidates:
            candidates.append(c.access_token)
        for tok in candidates:
            self.kite.set_access_token(tok)
            try:
                self._call(self.kite.profile, retries=1)
                self._creds = replace(c, access_token=tok)
                log.info("Authenticated with existing access token")
                return
            except kex.TokenException:
                log.warning("Access token rejected (expired or invalid)")
        if c.request_token and c.api_secret:
            data = self.kite.generate_session(c.request_token, api_secret=c.api_secret)
            self.kite.set_access_token(data["access_token"])
            self._creds = replace(c, access_token=data["access_token"], request_token=None)
            log.info("New session created from request token")
            return
        raise AuthError("No valid Kite session. Complete today's login via the link sent by "
                        "token_service (or run: python token_service.py url)")

    # ------------------------------------------------------- generic caller
    def _call(self, fn: Callable, *args, retries: int = 4, limiter: Optional[_RateLimiter] = None, **kw):
        limiter = limiter or self._data_limit
        for attempt in range(retries + 1):
            limiter.wait()
            try:
                return fn(*args, **kw)
            except kex.TokenException:
                raise  # fatal: needs a new login
            except _RETRYABLE as exc:
                if attempt == retries:
                    raise
                backoff = min(2 ** attempt, 20)
                log.warning("%s failed (%s); retry in %ss", getattr(fn, "__name__", fn), exc, backoff)
                time.sleep(backoff)

    # ----------------------------------------------------------- instruments
    def load_instruments(self) -> None:
        bfo = self._call(self.kite.instruments, "BFO")
        self._contracts = [
            OptionContract(
                token=i["instrument_token"], symbol=i["tradingsymbol"], exchange="BFO",
                strike=float(i["strike"]), expiry=i["expiry"], kind=i["instrument_type"],
                lot_size=int(i["lot_size"]), tick_size=float(i["tick_size"]),
            )
            for i in bfo
            if i.get("name") == "SENSEX" and i["instrument_type"] in ("CE", "PE")
        ]
        if not self._contracts:
            raise RuntimeError("No SENSEX options found in the BFO instruments dump")
        self.spot_token = self._find_index("BSE", "SENSEX")
        self.vix_token = self._find_index("NSE", "INDIA VIX")
        self._keys[self.spot_token] = "BSE:SENSEX"
        self._keys[self.vix_token] = "NSE:INDIA VIX"
        log.info("Loaded %d SENSEX option contracts", len(self._contracts))

    def _find_index(self, exchange: str, symbol: str) -> int:
        for i in self._call(self.kite.instruments, exchange):
            if i["tradingsymbol"] == symbol:
                return i["instrument_token"]
        raise RuntimeError(f"Index {exchange}:{symbol} not found in instruments dump")

    def expiries(self) -> list[dt.date]:
        return sorted({c.expiry for c in self._contracts})

    def contracts_for(self, expiry: dt.date) -> list[OptionContract]:
        return [c for c in self._contracts if c.expiry == expiry]

    # ------------------------------------------------------------- REST data
    def quote(self, keys: list[str]) -> dict:
        out: dict = {}
        for i in range(0, len(keys), 400):  # quote() caps at 500 instruments
            out.update(self._call(self.kite.quote, keys[i:i + 400]))
        return out

    def historical(self, token: int, start: dt.datetime, end: dt.datetime, interval: str) -> list[dict]:
        """Requires the Kite historical-data add-on subscription."""
        return self._call(self.kite.historical_data, token, start, end, interval)

    def available_cash(self) -> float:
        m = self._call(self.kite.margins, "equity")
        return float(m["available"]["live_balance"])

    def required_margin(self, legs: Iterable[tuple[OptionContract, str, int]], product: str) -> Optional[float]:
        orders = [
            {"exchange": c.exchange, "tradingsymbol": c.symbol, "transaction_type": side,
             "variety": "regular", "product": product, "order_type": "MARKET", "quantity": qty}
            for c, side, qty in legs
        ]
        try:
            res = self._call(self.kite.basket_order_margins, orders, consider_positions=True)
            return float(res["final"]["total"])
        except Exception as exc:  # noqa: BLE001
            log.error("Margin check failed: %s", exc)
            return None

    def net_positions(self) -> dict[str, int]:
        """Open SENSEX option positions at the broker: tradingsymbol -> signed quantity."""
        symbols = {c.symbol for c in self._contracts}
        out: dict[str, int] = {}
        for p in self._call(self.kite.positions)["net"]:
            if p["exchange"] == "BFO" and p["tradingsymbol"] in symbols and int(p["quantity"]) != 0:
                out[p["tradingsymbol"]] = int(p["quantity"])
        return out

    # ------------------------------------------------------------- streaming
    def start_stream(self) -> None:
        t = KiteTicker(self._creds.api_key, self._creds.access_token)
        t.on_ticks = self._on_ticks
        t.on_connect = self._on_connect
        t.on_close = lambda ws, code, reason: (self._ws_connected.clear(), log.warning("WS closed %s %s", code, reason))
        t.on_error = lambda ws, code, reason: log.error("WS error %s %s", code, reason)
        t.on_reconnect = lambda ws, n: log.warning("WS reconnect attempt %s", n)
        t.on_noreconnect = self._on_noreconnect
        self._ticker = t
        self.stream_dead = False
        self.subscribe_tokens({self.spot_token: "BSE:SENSEX", self.vix_token: "NSE:INDIA VIX"})
        t.connect(threaded=True)  # KiteTicker auto-reconnects with backoff

    def stop_stream(self) -> None:
        t = self._ticker
        if t is not None:
            try:
                t.stop_retry()
                t.close()
            except Exception:  # noqa: BLE001
                log.debug("stream close error", exc_info=True)
        self._ticker, self._subs, self._ticks = None, set(), {}
        self._ws_connected.clear()
        self.stream_dead = False

    def _on_connect(self, ws, _resp) -> None:
        self._ws_connected.set()
        if self._subs:
            ws.subscribe(list(self._subs))
            ws.set_mode(ws.MODE_LTP, list(self._subs))
        log.info("WebSocket connected; %d tokens", len(self._subs))

    def _on_noreconnect(self, _ws) -> None:
        self.stream_dead = True
        log.critical("WebSocket gave up reconnecting; falling back to REST polling")

    def _on_ticks(self, _ws, ticks: list[dict]) -> None:
        now = time.monotonic()
        for t in ticks:
            self._ticks[t["instrument_token"]] = (t["last_price"], now)

    def subscribe_tokens(self, mapping: dict[int, str]) -> None:
        self._keys.update(mapping)
        new = set(mapping) - self._subs
        self._subs |= new
        if new and self._ticker is not None and self._ws_connected.is_set():
            self._ticker.subscribe(list(new))
            self._ticker.set_mode(self._ticker.MODE_LTP, list(new))

    def market_is_live(self, max_age_s: float = 30.0) -> bool:
        """True when the SENSEX index has ticked recently.

        On exchange holidays and outside market hours no ticks arrive, so the
        bot idles instead of trading on stale prices.
        """
        tick = self._ticks.get(self.spot_token)
        return bool(tick) and time.monotonic() - tick[1] <= max_age_s

    def get_ltp(self, token: int, max_age_s: float = 5.0) -> float:
        """Streamed LTP if fresh, else REST fallback."""
        tick = self._ticks.get(token)
        if tick and time.monotonic() - tick[1] <= max_age_s:
            return tick[0]
        key = self._keys[token]
        return float(self.quote([key])[key]["last_price"])

    # ---------------------------------------------------------------- orders
    def place_order(self, c: OptionContract, side: str, qty: int, product: str,
                    price: float, tag: str) -> str:
        """No automatic retry: a network error may still have placed the order.
        Callers reconcile via order_history/positions() using the tag."""
        return self._call(
            self.kite.place_order, retries=0, limiter=self._order_limit,
            variety=KiteConnect.VARIETY_REGULAR, exchange=c.exchange, tradingsymbol=c.symbol,
            transaction_type=side, quantity=qty, product=product,
            order_type=KiteConnect.ORDER_TYPE_LIMIT, price=price,
            validity=KiteConnect.VALIDITY_DAY, tag=tag[:20],
        )

    def modify_order(self, order_id: str, price: float) -> str:
        return self._call(self.kite.modify_order, variety=KiteConnect.VARIETY_REGULAR,
                          order_id=order_id, price=price, limiter=self._order_limit)

    def cancel_order(self, order_id: str) -> str:
        return self._call(self.kite.cancel_order, variety=KiteConnect.VARIETY_REGULAR,
                          order_id=order_id, limiter=self._order_limit)

    def order_history(self, order_id: str) -> list[dict]:
        return self._call(self.kite.order_history, order_id)
