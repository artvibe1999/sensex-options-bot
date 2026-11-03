"""Order execution (brief section 8): order state machine, paper broker, live adapter, execution engine.

Every order: a unique client id (also the Kite tag, so a timed-out placement can be found again), persisted as an
intent (CREATED) BEFORE the broker is called, then every state change and fill journaled. Requested, filled and
remaining quantity are tracked separately. Retries never create a second order for the same intent: a live retry
first searches the broker's order book for the tag.

Mode: the PaperBroker is used unless the bot was started in LIVE mode (Telegram /mode live + /confirm AND
LIVE_TRADING_CONFIRM=YES in .env). In PAPER mode the live adapter is never constructed, so no code path can reach the
Kite order endpoints.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .models import IST, OptionQuote
from .settings import LiveExec, PaperSim

log = logging.getLogger(__name__)

CREATED, SUBMITTED, ACKNOWLEDGED = "CREATED", "SUBMITTED", "ACKNOWLEDGED"
PARTIALLY_FILLED, FILLED, CANCEL_PENDING = "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING"
CANCELLED, REJECTED, UNKNOWN = "CANCELLED", "REJECTED", "UNKNOWN"
STATES = (CREATED, SUBMITTED, ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, UNKNOWN)
TERMINAL = frozenset((FILLED, CANCELLED, REJECTED))

TRANSITIONS = {
    CREATED: {SUBMITTED, REJECTED, CANCELLED, UNKNOWN},
    SUBMITTED: {ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, UNKNOWN},
    ACKNOWLEDGED: {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, UNKNOWN},
    PARTIALLY_FILLED: {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, UNKNOWN},
    CANCEL_PENDING: {PARTIALLY_FILLED, FILLED, CANCELLED, UNKNOWN},
    UNKNOWN: set(STATES) - {CREATED},
    FILLED: set(), CANCELLED: set(), REJECTED: set(),
}


class InvalidTransition(RuntimeError):
    pass


def check_transition(old: str, new: str) -> None:
    if new not in TRANSITIONS.get(old, set()):
        raise InvalidTransition(f"order state {old} -> {new} is not allowed")


def round_tick(price: float, tick: float, up: bool) -> float:
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(max(n, 1) * tick, 2)


@dataclass(frozen=True)
class OrderRequest:
    client_id: str
    token: int
    exchange: str
    symbol: str
    side: str                       # BUY | SELL
    qty: int
    lot_size: int
    tick_size: float
    product: str
    market: bool = False            # live exits escalate to MARKET (with market protection)


@dataclass
class BrokerResult:
    events: list = field(default_factory=list)       # (state, cumulative filled qty, avg price, detail)
    broker_order_id: Optional[str] = None
    limit_price: Optional[float] = None

    @property
    def final(self) -> tuple[str, int, float]:
        if not self.events:
            return UNKNOWN, 0, 0.0
        s, q, p, _ = self.events[-1]
        return s, q, p


class Broker(Protocol):
    mode: str

    def submit(self, req: OrderRequest) -> BrokerResult: ...

    def lookup(self, client_id: str) -> Optional[BrokerResult]: ...


# ---------------------------------------------------------------------------- paper
class PaperBroker:
    """Fills against the CURRENT executable quote: BUY at ask + slippage, SELL at bid - slippage (tick-rounded against
    us). Simulates rejections, no-fills and partial fills (whole lots) with configured probabilities."""
    mode = "PAPER"

    def __init__(self, sim: PaperSim, quote_fn: Callable[[int], Optional[OptionQuote]], stale_s: float = 5.0,
                 rng: Optional[random.Random] = None, now_mono: Callable[[], float] = time.monotonic) -> None:
        self.sim, self.quote_fn, self.stale_s = sim, quote_fn, stale_s
        self.rng = rng or random.Random(sim.seed)
        self.now_mono = now_mono

    def submit(self, req: OrderRequest) -> BrokerResult:
        r = BrokerResult(broker_order_id=f"PAPER-{req.client_id}")
        q = self.quote_fn(req.token)
        why = q.problem(self.now_mono(), self.stale_s) if q is not None else "no quote"
        if why:
            r.events.append((REJECTED, 0, 0.0, f"paper: no executable quote ({why})"))
            return r
        r.events.append((ACKNOWLEDGED, 0, 0.0, f"ack after {self.sim.ack_delay_s:.1f}s (simulated)"))
        u = self.rng.random()
        if u < self.sim.reject_prob:
            r.events.append((REJECTED, 0, 0.0, "paper: simulated rejection"))
            return r
        if u < self.sim.reject_prob + self.sim.no_fill_prob:
            r.events.append((CANCEL_PENDING, 0, 0.0, "paper: not filled within the timeout"))
            r.events.append((CANCELLED, 0, 0.0, "paper: simulated no-fill"))
            return r
        if req.side == "BUY":
            px = round_tick(q.ask * (1 + self.sim.entry_slippage_pct / 100), req.tick_size, up=True)
        else:
            px = round_tick(q.bid * (1 - self.sim.exit_slippage_pct / 100), req.tick_size, up=False)
        r.limit_price = px
        lots = req.qty // req.lot_size
        partial = lots > 1 and self.rng.random() < self.sim.partial_fill_prob
        if partial:
            got = self.rng.randint(1, lots - 1) * req.lot_size
            r.events.append((PARTIALLY_FILLED, got, px, f"paper: partial {got}/{req.qty}"))
            r.events.append((CANCEL_PENDING, got, px, "paper: remainder not filled"))
            r.events.append((CANCELLED, got, px, f"paper: remainder {req.qty - got} cancelled"))
            return r
        r.events.append((FILLED, req.qty, px, "paper fill"))
        return r

    def lookup(self, client_id: str) -> Optional[BrokerResult]:
        return None                                   # paper orders live only in the journal


# ---------------------------------------------------------------------------- live
_KITE_STATE = {"COMPLETE": FILLED, "REJECTED": REJECTED, "CANCELLED": CANCELLED}


class LiveBroker:
    """Real Kite orders. Constructed ONLY in LIVE mode. Marketable limit orders (or MARKET with market protection for
    escalated exits); polls until final or the timeout, then cancels the remainder and waits for the final state."""
    mode = "LIVE"

    def __init__(self, client, cfg: LiveExec, quote_fn: Callable[[int], Optional[OptionQuote]],
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.client, self.cfg, self.quote_fn, self._sleep = client, cfg, quote_fn, sleep

    def _state(self, oid: str) -> tuple[str, int, float, str]:
        h = self.client.order_history(oid)[-1]
        st = str(h.get("status") or "")
        filled = int(h.get("filled_quantity") or 0)
        avg = float(h.get("average_price") or 0)
        mapped = _KITE_STATE.get(st)
        if mapped is None:
            mapped = PARTIALLY_FILLED if filled > 0 else ACKNOWLEDGED
        return mapped, filled, avg, f"kite {st} {h.get('status_message') or ''}".strip()

    def _follow(self, oid: str, r: BrokerResult, deadline: float) -> None:
        last = None
        while True:
            try:
                st = self._state(oid)
            except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
                log.warning("order_history failed for %s: %s", oid, exc.__class__.__name__)
                st = None
            if st and st != last:
                r.events.append(st)
                last = st
            if st and st[0] in TERMINAL:
                return
            if time.monotonic() >= deadline:
                break
            self._sleep(0.5)
        try:
            self.client.cancel_order(oid)
            r.events.append((CANCEL_PENDING, last[1] if last else 0, last[2] if last else 0.0, "timeout: cancel sent"))
        except Exception:  # noqa: BLE001
            log.exception("Cancel failed for %s", oid)
        for _ in range(10):
            try:
                st = self._state(oid)
            except Exception:  # noqa: BLE001
                st = None
            if st and st != last:
                r.events.append(st)
                last = st
            if st and st[0] in TERMINAL:
                return
            self._sleep(0.5)
        r.events.append((UNKNOWN, last[1] if last else 0, last[2] if last else 0.0, "final state not confirmed"))

    def submit(self, req: OrderRequest) -> BrokerResult:
        r = BrokerResult()
        existing = self.client.order_by_tag(req.client_id)      # idempotency: never place the same intent twice
        if existing:
            r.broker_order_id = existing
            r.events.append((ACKNOWLEDGED, 0, 0.0, "adopted existing order with this tag"))
            self._follow(existing, r, time.monotonic() + self.cfg.fill_timeout_s)
            return r
        price = 0.0
        if not req.market:
            q = self.quote_fn(req.token)
            if q is None or q.mid is None:
                r.events.append((REJECTED, 0, 0.0, "no executable quote for the limit price"))
                return r
            b = self.cfg.limit_buffer_pct / 100
            price = round_tick(q.ask * (1 + b), req.tick_size, True) if req.side == "BUY" else \
                round_tick(q.bid * (1 - b), req.tick_size, False)
        r.limit_price = price or None
        from zerodha_client import OptionContract          # local import: only LIVE mode ever reaches this
        c = OptionContract(req.token, req.symbol, req.exchange, 0.0, dt.date.today(), "CE", req.lot_size, req.tick_size)
        try:
            oid = self.client.place_order(c, req.side, req.qty, req.product, price, req.client_id, market=req.market)
        except Exception as exc:  # noqa: BLE001 - a network error may still have placed it: search by tag
            oid = None
            try:
                oid = self.client.order_by_tag(req.client_id)
            except Exception:  # noqa: BLE001
                log.exception("Order book search failed after a placement error")
            if not oid:
                r.events.append((UNKNOWN if "timeout" in str(exc).lower() else REJECTED, 0, 0.0,
                                 f"placement failed: {exc.__class__.__name__}: {str(exc)[:120]}"))
                return r
        r.broker_order_id = str(oid)
        r.events.append((ACKNOWLEDGED, 0, 0.0, f"kite order {oid}"))
        self._follow(str(oid), r, time.monotonic() + self.cfg.fill_timeout_s)
        return r

    def lookup(self, client_id: str) -> Optional[BrokerResult]:
        oid = self.client.order_by_tag(client_id)
        if not oid:
            return None
        r = BrokerResult(broker_order_id=oid)
        st = self._state(oid)
        r.events.append(st)
        if st[0] not in TERMINAL:
            self._follow(oid, r, time.monotonic())        # still working at the broker: cancel, wait for final
        return r


# ---------------------------------------------------------------------------- engine
@dataclass
class Execution:
    client_id: str
    state: str
    requested: int
    filled: int
    avg_price: float
    detail: str = ""

    @property
    def remaining(self) -> int:
        return self.requested - self.filled


class ExecutionEngine:
    """Single entry point for orders. Journals the intent first, then submits, then applies every broker event
    through the state machine."""

    def __init__(self, store, broker: Broker, product: str, tag_prefix: str, clock=None) -> None:
        self.store, self.broker, self.product, self.prefix = store, broker, product, tag_prefix
        self.clock = clock

    @property
    def mode(self) -> str:
        return self.broker.mode

    def new_client_id(self, kind: str, today: dt.date) -> str:
        base = f"{self.prefix}{today:%y%m%d}{'E' if kind == 'entry' else 'X'}"
        n = self.store.next_order_seq(base)
        return f"{base}{n:04d}"[:20]

    def execute(self, *, kind: str, position_id: str, index: str, token: int, exchange: str, symbol: str, side: str,
                qty: int, lot_size: int, tick_size: float, reason: str, emergency: bool = False,
                market: bool = False, today: Optional[dt.date] = None) -> Execution:
        if qty <= 0 or qty % lot_size:
            raise ValueError(f"quantity {qty} is not a whole number of lots of {lot_size}")
        today = today or dt.datetime.now(IST).date()
        cid = self.new_client_id(kind, today)
        self.store.order_intent({"client_id": cid, "mode": self.mode, "position_id": position_id, "index": index,
                                 "symbol": symbol, "token": token, "exchange": exchange, "side": side, "kind": kind,
                                 "requested_qty": qty, "reason": reason, "emergency": emergency})
        req = OrderRequest(cid, token, exchange, symbol, side, qty, lot_size, tick_size, self.product, market)
        self.store.order_update(cid, SUBMITTED, f"{side} {qty} {symbol} ({reason})")
        try:
            res = self.broker.submit(req)
        except Exception as exc:  # noqa: BLE001 - outcome unknown: reconciliation resolves it
            log.exception("Broker submit raised for %s", cid)
            res = BrokerResult(events=[(UNKNOWN, 0, 0.0, f"submit raised {exc.__class__.__name__}")])
        return self._apply(cid, qty, res)

    def _apply(self, cid: str, qty: int, res: BrokerResult) -> Execution:
        state, filled, avg = SUBMITTED, 0, 0.0
        detail = ""
        for st, cum, px, det in res.events:
            if st == state and cum == filled:
                continue                                  # repeated report of the same state
            try:
                check_transition(state, st)
            except InvalidTransition:
                log.error("Ignoring impossible broker transition %s -> %s for %s", state, st, cid)
                continue
            if cum > filled:
                # journal the increment as one fill at the incremental average price
                inc = cum - filled
                inc_px = ((px * cum) - (avg * filled)) / inc if filled else px
                self.store.fill(cid, inc, round(inc_px, 2))
                avg = px
                filled = cum
            state, detail = st, det
            self.store.order_update(cid, st, det, filled_qty=filled, avg_price=avg if filled else None,
                                    broker_order_id=res.broker_order_id, limit_price=res.limit_price)
        if state not in TERMINAL and state != UNKNOWN:
            # a broker that stopped reporting: we must not assume anything
            self.store.order_update(cid, UNKNOWN, "no final state from broker", filled_qty=filled)
            state = UNKNOWN
        return Execution(cid, state, qty, filled, avg, detail)

    def reconcile_open_orders(self) -> list[Execution]:
        """Startup / reconnect: resolve every non-terminal order of this mode. Paper orders that were in flight
        during a crash never reached a broker: they are cancelled with whatever fills the journal holds."""
        out = []
        for o in self.store.open_orders(self.mode):
            cid = o["client_id"]
            res = None
            try:
                res = self.broker.lookup(cid)
            except Exception:  # noqa: BLE001
                log.exception("Lookup failed for %s", cid)
            if res is None or not res.events:
                filled = int(o.get("filled_qty") or 0)
                if self.mode == "LIVE" and res is None:
                    # not in the broker's book: the placement never arrived
                    self.store.order_update(cid, CANCELLED, "not found at broker on reconciliation")
                else:
                    self.store.order_update(cid, CANCELLED, "in flight during restart: cancelled (journal fills kept)")
                out.append(Execution(cid, CANCELLED, o["requested_qty"], filled, o.get("avg_price") or 0.0,
                                     "reconciled"))
                continue
            st, cum, px, det = res.events[-1]
            self.store.order_update(cid, st if st in TERMINAL else UNKNOWN, f"reconciled: {det}", filled_qty=cum,
                                    avg_price=px or None, broker_order_id=res.broker_order_id)
            out.append(Execution(cid, st, o["requested_qty"], cum, px, det))
        return out
