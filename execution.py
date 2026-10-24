"""Multi-leg order execution (paper and live).

Kite has no atomic server-side basket execution, so a multi-leg structure is placed in two verified phases:

  1. every BUY leg is placed, and execution WAITS until each is confirmed COMPLETE;
  2. only then are the SELL legs placed.

The long legs therefore exist before the short legs, so the exchange recognises the hedge and grants spread margin
benefit, and a failure part-way leaves at most a long (limited-loss) position, never a naked short. Any failure
(rejection, timeout, partial fill, network error) cancels what is open and unwinds what already filled, longs last.

The same rule applies to exits: a closing order is the REVERSE of the entry (BUY<->SELL), so the shorts are bought
back first, then the longs sold, which never leaves a naked short behind. Exits start as marketable limit orders
(widening the buffer after each failed attempt) and escalate to MARKET orders after
`ExecutionParams.market_exit_after_failures` failures.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from zerodha_client import OptionContract

log = logging.getLogger("execution")

BUY, SELL = "BUY", "SELL"


@dataclass(frozen=True)
class OrderLeg:
    contract: OptionContract
    side: str
    qty: int
    ref_price: float


def round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)


def reverse_legs(legs: Sequence[OrderLeg], marks: Optional[dict[int, float]] = None) -> list[OrderLeg]:
    """Clone a structure with every side flipped (BUY->SELL, SELL->BUY), same quantities.
    `marks` (token -> LTP) replaces the reference price when available."""
    return [OrderLeg(l.contract, SELL if l.side == BUY else BUY, l.qty,
                     (marks or {}).get(l.contract.token, l.ref_price)) for l in legs]


# ------------------------------------------------- pre-trade structure validation
@dataclass(frozen=True)
class QuoteCheck:
    ok: bool
    reason: str = ""


def validate_plan_quotes(plan, chain, max_spread_pct: float = 8.0, max_loss_slack: float = 1.15) -> QuoteCheck:
    """Re-validate a planned structure against the CURRENT chain right before sizing/ordering.

    Every leg must be a listed, quoted contract of the plan's expiry; where depth is available the bid/ask must be
    two-sided and tight (a SELL leg with no bid, or a wide spread, cannot be exited); and the structure must still be
    defined-risk with positive reward when priced at EXECUTABLE prices (BUY at the ask, SELL at the bid) with a
    maximum loss no worse than `max_loss_slack` x the planned one. Fails closed."""
    from option_strategies import analyse
    priced = []
    seen = set()
    for l in plan.legs:
        c = l.contract
        key = (c.kind, c.strike, l.side)
        if key in seen:
            return QuoteCheck(False, f"duplicate leg {c.symbol}")
        seen.add(key)
        q = chain.get(c.strike, c.kind)
        if q is None or q.contract.symbol != c.symbol:
            return QuoteCheck(False, f"{c.symbol} is not currently quoted")
        if c.expiry != plan.expiry or chain.expiry != plan.expiry:
            return QuoteCheck(False, f"{c.symbol} expiry mismatch")
        px = q.ltp
        if q.bid is not None and q.ask is not None:
            if q.ask <= 0 or (l.side != BUY and q.bid <= 0):
                return QuoteCheck(False, f"{c.symbol} has no {'bid' if l.side != BUY else 'ask'}")
            if q.bid > 0:
                mid = (q.bid + q.ask) / 2
                spread = (q.ask - q.bid) / mid * 100
                if spread > max_spread_pct:
                    return QuoteCheck(False, f"{c.symbol} spread {spread:.1f}% > {max_spread_pct:.0f}%")
            px = q.ask if l.side == BUY else q.bid
        priced.append((c.kind, c.strike, l.side, l.ratio, px))
    eco = analyse(priced)
    if eco is None:
        return QuoteCheck(False, "structure has unbounded loss at executable prices")
    if eco.max_profit <= 0 or eco.max_loss <= 0:
        return QuoteCheck(False, "no positive reward at executable prices")
    if eco.max_loss > plan.max_loss_unit * max_loss_slack:
        return QuoteCheck(False, f"max loss {eco.max_loss:.1f} at executable prices exceeds plan {plan.max_loss_unit:.1f}")
    return QuoteCheck(True)


def apply_exit_fills(open_legs: list, fills: dict[str, tuple[float, int]]) -> tuple[float, list]:
    """Book exit fills against open legs: reduces each leg's qty by what actually closed.
    fills: symbol -> (avg exit price, filled qty). Returns (gross P&L of the closed part, fills for costing)."""
    gross, cash = 0.0, []
    for l in open_legs:
        r = fills.get(l.contract.symbol)
        if not r:
            continue
        px, q = r[0], min(int(r[1]), l.qty)
        if q <= 0:
            continue
        gross += (px - l.entry_price) * q * (1 if l.side == BUY else -1)
        cash += [(l.side, l.entry_price, q), (SELL if l.side == BUY else BUY, px, q)]
        l.qty -= q
    return gross, cash


# ------------------------------------------------------------------ margin
@dataclass(frozen=True)
class MarginCheck:
    ok: bool
    required: Optional[float]
    available: float
    reason: str = ""


def validate_margin(client, legs: Sequence[OrderLeg], product: str, available_cash: float,
                    utilisation: float = 0.8) -> MarginCheck:
    """Ask Kite (basket_order_margins, positions considered) what the whole structure needs and compare with cash.
    Fails closed: a failed margin query blocks the trade."""
    req = client.required_margin([(l.contract, l.side, l.qty) for l in legs], product)
    if req is None:
        return MarginCheck(False, None, available_cash, "margin query failed")
    if req > available_cash * utilisation:
        return MarginCheck(False, req, available_cash,
                           f"needs {req:,.0f} > {utilisation:.0%} of cash {available_cash:,.0f}")
    return MarginCheck(True, req, available_cash)


# ------------------------------------------------------------------ paper
class PaperBroker:
    """Simulated fills at the reference price +/- slippage; no API orders are placed."""

    def __init__(self, slippage_pct: float) -> None:
        self.slip = slippage_pct

    def execute(self, legs: list[OrderLeg], product: str, buffer_mult: float = 1.0,
                market: bool = False) -> Optional[dict[str, float]]:
        fills = {}
        for l in sorted(legs, key=lambda x: x.side != BUY):
            f = l.ref_price * (1 + self.slip if l.side == BUY else 1 - self.slip)
            fills[l.contract.symbol] = round_tick(f, l.contract.tick_size)
            log.info("[PAPER] %s %s x%d @ %.2f", l.side, l.contract.symbol, l.qty, fills[l.contract.symbol])
        return fills

    def close(self, legs: list[OrderLeg], product: str, buffer_mult: float = 1.0,
              market: bool = False) -> dict[str, tuple[float, int]]:
        fills = self.execute(legs, product, buffer_mult, market) or {}
        return {l.contract.symbol: (fills[l.contract.symbol], l.qty) for l in legs if l.contract.symbol in fills}


# ------------------------------------------------------------------- live
class LiveBroker:
    """Real orders through a Kite-like client: place_order, order_history, cancel_order."""

    def __init__(self, client, cfg, sleep: Callable[[float], None] = time.sleep) -> None:
        self.client, self.x, self._sleep = client, cfg.execution, sleep

    def _limit(self, l: OrderLeg, mult: float = 1.0) -> float:
        b = min(self.x.limit_buffer_pct * mult, self.x.max_exit_buffer_pct)
        return round_tick(l.ref_price * (1 + b if l.side == BUY else 1 - b), l.contract.tick_size)

    def _place(self, l: OrderLeg, product: str, mult: float, market: bool) -> str:
        return self.client.place_order(l.contract, l.side, l.qty, product, self._limit(l, mult), self.x.tag,
                                       market=market)

    def _state(self, order_id: str) -> tuple[str, float, int, str]:
        h = self.client.order_history(order_id)[-1]
        return (h["status"], float(h.get("average_price") or 0), int(h.get("filled_quantity") or 0),
                str(h.get("status_message") or ""))

    def _settle(self, orders: dict[str, OrderLeg]) -> dict[str, tuple[str, float, int, str]]:
        """Poll until every order is final (COMPLETE/REJECTED/CANCELLED) or the fill timeout passes."""
        deadline = time.monotonic() + self.x.fill_timeout_s
        res: dict[str, tuple[str, float, int, str]] = {}
        pending = dict(orders)
        while pending:
            for oid in list(pending):
                try:
                    res[oid] = self._state(oid)
                except Exception:  # noqa: BLE001 - transient API/network error: keep polling until the deadline
                    log.warning("order_history failed for %s", oid, exc_info=True)
                    continue
                if res[oid][0] in ("COMPLETE", "REJECTED", "CANCELLED"):
                    pending.pop(oid)
            if not pending or time.monotonic() >= deadline:
                break
            self._sleep(0.5)
        return res

    def _phase(self, legs: list[OrderLeg], product: str, mult: float, market: bool,
               done: list[tuple[OrderLeg, float, int]]) -> bool:
        """Place all `legs`, wait for every fill. Appends whatever filled to `done`. True only if all COMPLETE."""
        orders: dict[str, OrderLeg] = {}
        placed_ok = True
        for l in legs:
            try:
                orders[self._place(l, product, mult, market)] = l
            except Exception as exc:  # noqa: BLE001 - network timeout / margin rejection / exchange error
                log.error("Order placement failed for %s %s: %s", l.side, l.contract.symbol, exc)
                placed_ok = False
                break
        res = self._settle(orders)
        all_complete = placed_ok
        for oid, l in orders.items():
            status, avg, filled, msg = res.get(oid, ("UNKNOWN", 0.0, 0, ""))
            if status == "COMPLETE" and filled < l.qty:
                status = "SHORT_FILL"                          # never trust COMPLETE with fewer lots than ordered
            if status != "COMPLETE":
                all_complete = False
                if status not in ("REJECTED", "CANCELLED"):
                    try:
                        self.client.cancel_order(oid)
                    except Exception:  # noqa: BLE001
                        log.exception("Cancel failed for %s", oid)
                    try:
                        status, avg, filled, msg = self._state(oid)     # a partial fill may have landed
                    except Exception:  # noqa: BLE001
                        log.exception("Could not re-read order %s after cancel", oid)
                log.error("Leg %s %s not filled: status=%s filled=%d/%d %s", l.side, l.contract.symbol,
                          status, filled, l.qty, msg)
            if filled:
                done.append((l, avg, filled))
            if status == "COMPLETE":
                log.info("[LIVE] %s %s x%d @ %.2f (order %s)", l.side, l.contract.symbol, l.qty, avg, oid)
        return all_complete

    def execute(self, legs: list[OrderLeg], product: str, buffer_mult: float = 1.0,
                market: bool = False) -> Optional[dict[str, float]]:
        """Two-phase placement. Returns {symbol: fill price} when every leg filled completely, else None
        (after unwinding anything that did fill)."""
        buys = [l for l in legs if l.side == BUY]
        sells = [l for l in legs if l.side != BUY]
        done: list[tuple[OrderLeg, float, int]] = []
        if not self._phase(buys, product, buffer_mult, market, done):
            self._unwind(done, product)
            return None
        if sells and not self._phase(sells, product, buffer_mult, market, done):
            self._unwind(done, product)
            return None
        return {l.contract.symbol: avg for l, avg, _ in done}

    def close(self, legs: list[OrderLeg], product: str, buffer_mult: float = 1.0,
              market: bool = False) -> dict[str, tuple[float, int]]:
        """Close a structure. Unlike entry there is NO unwind: whatever closed stays closed and the caller retries
        the remainder. Shorts are bought back first; the longs are only sold once every buy-back is confirmed, so the
        hedge is never removed while a short is still open. Returns {symbol: (avg price, filled qty)}."""
        buys = [l for l in legs if l.side == BUY]
        sells = [l for l in legs if l.side != BUY]
        done: list[tuple[OrderLeg, float, int]] = []
        if self._phase(buys, product, buffer_mult, market, done) and sells:
            self._phase(sells, product, buffer_mult, market, done)
        out: dict[str, tuple[float, int]] = {}
        for l, avg, q in done:
            if l.contract.symbol in out:                      # defensive: combine repeated fills of one symbol
                a0, q0 = out[l.contract.symbol]
                out[l.contract.symbol] = ((a0 * q0 + avg * q) / (q0 + q), q0 + q)
            else:
                out[l.contract.symbol] = (avg, q)
        return out

    def _unwind(self, done: list[tuple[OrderLeg, float, int]], product: str) -> None:
        """Reverse what filled: buy back shorts first, then sell longs. Marketable limit with a wide buffer."""
        rev = reverse_legs([OrderLeg(l.contract, l.side, q, l.ref_price) for l, _a, q in done])
        for r in sorted(rev, key=lambda x: x.side != BUY):
            try:
                oid = self.client.place_order(r.contract, r.side, r.qty, product, self._limit(r, 3.0),
                                              self.x.tag, market=False)
                log.warning("Unwind order %s: %s %s x%d", oid, r.side, r.contract.symbol, r.qty)
            except Exception:  # noqa: BLE001
                log.critical("UNWIND FAILED for %s: close manually!", r.contract.symbol, exc_info=True)
