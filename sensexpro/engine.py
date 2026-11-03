"""Trading loop (brief sections 4-8 wired together) and restart recovery.

Per loop (1 s):
  1. refresh every index (candles, option window, quotes);
  2. with a position: emergency protection FIRST (independent, overrides everything), then the adaptive exit engine;
  3. without one: each new completed 5-minute bar -> regime -> ensemble -> scored signals (journaled); the best
     ELIGIBLE signal across all indices -> contract selection -> final score with option liquidity -> risk approval
     -> one order through the execution engine.
On start-up / reconnect: journal reconciliation (orders, position quantity from fills, broker positions in LIVE)
before any new entry is allowed.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from costs import CostParams, trade_cost

from .emergency import EmergencyProtection, EmergencyTrigger
from .execution import UNKNOWN, ExecutionEngine
from .exits import EXIT, REDUCE, AdaptiveExitEngine, OptionTracker, update_structure
from .market import IndexMarket
from .models import IST, OptionQuote, Position, Signal
from .options import select_contract
from .regime import RegimeState, detect
from .risk import EntryContext, RiskBook, approve_entry
from .settings import RiskLimits, Settings
from .strategies.ensemble import ELIGIBLE, Ensemble

log = logging.getLogger("sensexpro")

TRANSIENT = ("stale", "no quote", "no bid", "no ask")


class Notifier(Protocol):
    def alert(self, text: str, level: str = "INFO", key: Optional[str] = None) -> None: ...
    def order_row(self, row: list) -> None: ...
    def trade_row(self, row: list) -> None: ...
    def status_row(self, event: str, details: str) -> None: ...
    def journal_trade(self, ts: str, variant: str, event: str, reason: str, gross: float, cost: float, net: float,
                      payload: dict) -> None: ...


class NullNotifier:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.orders: list[list] = []
        self.trades: list[list] = []
        self.journal: list[tuple] = []

    def alert(self, text: str, level: str = "INFO", key: Optional[str] = None) -> None:
        self.alerts.append((level, text))

    def order_row(self, row: list) -> None:
        self.orders.append(row)

    def trade_row(self, row: list) -> None:
        self.trades.append(row)

    def status_row(self, event: str, details: str) -> None:
        pass

    def journal_trade(self, *a, **k) -> None:
        self.journal.append(a)


@dataclass
class Pending:
    signal: Signal
    expires: dt.datetime
    first_seen: float


def variant_of(pos_or_sig) -> str:
    if isinstance(pos_or_sig, Position):
        return f"sp_{pos_or_sig.strategy}_{pos_or_sig.direction}"
    c = pos_or_sig.candidate
    return f"sp_{c.strategy}_{c.direction}"


class TradingEngine:
    EXIT_RETRY_S = 3.0
    REST_QUOTE_EVERY_S = 5.0
    BROKER_CHECK_S = 30.0
    MAX_ENTRY_FAILURES = 3

    def __init__(self, s: Settings, mode: str, store, markets: dict[str, IndexMarket], broker, costs: CostParams,
                 notifier: Notifier, lots_fn: Callable[[str], int], max_lots: int,
                 broker_positions_fn: Optional[Callable[[], dict]] = None,
                 mono: Callable[[], float] = time.monotonic, state_file: Optional[str] = None) -> None:
        self.s, self.mode, self.store, self.markets = s, mode, store, markets
        self.costs, self.n, self.lots_fn, self.max_lots = costs, notifier, lots_fn, max_lots
        self.broker_positions_fn = broker_positions_fn
        self.mono, self.state_file = mono, state_file
        self.exe = ExecutionEngine(store, broker, s.execution.product, s.execution.tag_prefix)
        self.base_limits: RiskLimits = s.risk
        self.limits: RiskLimits = s.risk
        self.book = RiskBook.restore(store.get_state(mode, "risk_book"))
        self.ens = Ensemble(s.strategy)
        self.exits = AdaptiveExitEngine(s.strategy.exits, s.strategy.regime)
        self.emergency = EmergencyProtection(self.limits, s.session)
        self.position: Optional[Position] = None
        self.tracker = OptionTracker()
        self.pending: list[Pending] = []
        self.reconciled = False
        self.session_day: Optional[dt.date] = None
        self.regimes: dict[str, RegimeState] = {}
        self.last_assessment = None
        self.entry_failures = 0
        self._next_exit_try = 0.0
        self._last_rest = 0.0
        self._last_broker_check = -1e9
        self._broker_ok = True
        self._last_save = 0.0
        self._last_status_log = 0.0
        self._why_not: dict[str, str] = {}

    # ------------------------------------------------------------------ session
    def start_session(self, capital: float, today: dt.date) -> None:
        self.limits = self.base_limits.scaled(capital)
        self.emergency.set_limits(self.limits)
        self.book.ensure_base(capital)
        self.book.roll(today)
        self.session_day = today
        self.recover(today)
        self._save()
        log.info("Session %s %s: capital %.0f, limits x%.2f (premium cap %.0f, emergency %.0f, daily %.0f, weekly %.0f,"
                 " drawdown %.0f)", self.mode, today, capital, self.limits.scale, self.limits.max_premium_per_entry,
                 self.limits.emergency_loss_per_trade, self.limits.daily_loss_limit, self.limits.weekly_loss_limit,
                 self.limits.max_drawdown)

    def new_day(self, today: dt.date) -> None:
        if self.book.roll(today):
            self.pending.clear()
            self.entry_failures = 0
            self.store.app_event("new_day", {"day": today.isoformat(), "mode": self.mode})
            self._save()

    def recover(self, today: dt.date) -> None:
        """Reconcile journal and broker before any entry (brief 3.2, 8, 12.3)."""
        self.reconciled = False
        self.position = None                             # the journal is the truth: never re-save a stale copy
        execs = self.exe.reconcile_open_orders()
        if execs:
            log.warning("Reconciled %d in-flight order(s): %s", len(execs),
                        ", ".join(f"{e.client_id}={e.state}/{e.filled}" for e in execs))
        pos = self.store.open_position(self.mode)
        if pos is not None:
            pos = self._rebuild_from_fills(pos)
        if pos is not None and pos.entry_time.date() < today:
            self._close_stale(pos, today)
            pos = None
        self.position = pos
        unresolved = [o for o in self.store.open_orders(self.mode)]
        if unresolved:
            self.book.latch("unreconciled orders after restart", dt.datetime.now(IST))
            self.n.alert(f"{len(unresolved)} order(s) could not be resolved after restart: kill switch latched. "
                         "Check Kite, then /resetkill.", "CRITICAL", "recover-unresolved")
        if self.mode == "LIVE" and self.broker_positions_fn is not None:
            try:
                broker = self.broker_positions_fn()
                want = {pos.contract.symbol: pos.qty} if pos else {}
                if broker != want:
                    self.book.latch(f"broker positions {broker} != journal {want}", dt.datetime.now(IST))
                    self.n.alert(f"POSITION MISMATCH at start-up: broker {broker} vs journal {want}. Kill switch "
                                 "latched: reconcile in Kite, then /resetkill.", "CRITICAL", "recover-mismatch")
            except Exception:  # noqa: BLE001
                self.book.latch("broker positions unreadable at start-up", dt.datetime.now(IST))
                self.n.alert("Could not read broker positions at start-up: entries blocked (kill switch).",
                             "CRITICAL", "recover-positions")
        if self.position:
            log.warning("Recovered open position %s x%d (%s)", self.position.contract.symbol, self.position.qty,
                        self.position.status)
            self.n.alert(f"Recovered open position {self.position.index} {self.position.contract.symbol} "
                         f"x{self.position.qty} after restart: managing it.", "WARNING", "recover-pos")
        self.reconciled = True

    def _rebuild_from_fills(self, pos: Position) -> Optional[Position]:
        """Quantity and realised part of a position from its journaled fills (the journal is the truth)."""
        fills = self.store.position_fills(pos.id)
        eq = sum(q for k, side, q, px in fills if k == "entry")
        if eq <= 0:
            pos.status = "VOID"
            self.store.save_position(self.mode, pos, exit_reason="entry never filled")
            log.warning("Position %s had no entry fill: voided", pos.id)
            return None
        ev = sum(q * px for k, side, q, px in fills if k == "entry")
        xq = sum(q for k, side, q, px in fills if k == "exit")
        xv = sum(q * px for k, side, q, px in fills if k == "exit")
        pos.entry_price = ev / eq
        pos.qty = eq - xq
        pos.lots = max(pos.qty // pos.contract.lot_size, 0)
        if xq:
            cp = self.costs.for_exchange(pos.contract.exchange)
            pos.closed_qty = xq
            pos.closed_gross = xv - pos.entry_price * xq
            pos.closed_cost = trade_cost([("SELL", xv / xq, xq)], cp) + pos.entry_charges * xq / eq
            unbooked = (pos.closed_gross - pos.closed_cost) - self.book.booked.get(pos.id, 0.0)
            if abs(unbooked) > 0.01:                     # exit fills journaled but not yet in the risk book
                log.warning("Booking %.2f realised P&L of %s missed before the restart", unbooked, pos.id)
                self.book.add_realized(unbooked, pos.id)
        if pos.status == "PENDING":
            pos.status = "OPEN"
        if pos.qty <= 0:
            self._finalise(pos, "closed before restart (journal)", dt.datetime.now(IST))
            return None
        return pos

    def _close_stale(self, pos: Position, today: dt.date) -> None:
        """A position from an earlier day (the session ended without managing it). Paper: closed at its last seen
        bid (or entry price), clearly labelled. Live: MIS is squared off by the broker; flagged for manual check."""
        px = pos.last_bid or pos.entry_price
        cp = self.costs.for_exchange(pos.contract.exchange)
        gross = (px - pos.entry_price) * pos.qty
        cost = trade_cost([("SELL", px, pos.qty)], cp) + pos.entry_charges * pos.qty / max(pos.qty + pos.closed_qty, 1)
        pos.closed_gross += gross
        pos.closed_cost += cost
        pos.closed_qty += pos.qty
        self.book.add_realized(gross - cost, pos.id)
        pos.qty = 0
        reason = f"STALE_FROM_{pos.entry_time.date().isoformat()} (marked at last bid {px:.2f})"
        self._finalise(pos, reason, dt.datetime.now(IST))
        lvl = "CRITICAL" if self.mode == "LIVE" else "WARNING"
        self.n.alert(f"Position {pos.contract.symbol} from {pos.entry_time.date()} was still open at start-up: "
                     f"closed in the journal at {px:.2f}." + (" Verify the broker squared it off." if self.mode == "LIVE"
                                                               else ""), lvl, "stale-pos")

    # ------------------------------------------------------------------ main step
    def step(self, now: dt.datetime) -> None:
        for m in self.markets.values():
            m.refresh(now)
        if self.position is not None:
            self._manage(now)
        else:
            for name, m in self.markets.items():
                if m.consume_new_bar() and m.frame is not None:
                    self._on_bar(m, now)
            self._try_enter(now)
        if self.mono() - self._last_save > 15:
            self._save()

    # ------------------------------------------------------------------ signals
    def _on_bar(self, m: IndexMarket, now: dt.datetime) -> None:
        f = m.frame
        i = f.last
        b = f.bars[i]
        if b.start.date() != now.date() or (now - b.end).total_seconds() > 420:
            return                                       # warm-up history: never signal on an old bar
        spot_ok = m.spot() is not None
        rs = detect(f, i, self.s.strategy.regime, data_ok=m.data_ok and spot_ok,
                    liquidity_ok=m.liquidity_ok(now.date()),
                    why_unsafe=m.data_why if not m.data_ok else ("no live index tick" if not spot_ok else ""))
        self.regimes[m.spec.name] = rs
        res = self.ens.evaluate(f, i, rs, consumed=self.store.consumed(now.date()))
        for c, why in res.rejected:
            self.store.consume(now.date(), c.setup_key, why)
            log.info("SIGNAL %s %s %s rejected: %s", c.index, c.strategy, c.direction, why)
        for sig in res.signals:
            self.store.signal(now, sig)
            log.info("SIGNAL %s %s %s score %.0f (pre-liquidity) %s regime %s", sig.index, sig.candidate.strategy,
                     sig.direction, sig.score, sig.decision, sig.regime)
            if sig.decision != ELIGIBLE:
                self.store.consume(now.date(), sig.candidate.setup_key, sig.decision)
        if res.best is not None:
            ttl = dt.timedelta(seconds=self.s.strategy.ensemble.signal_ttl_s)
            self.pending.append(Pending(res.best, res.best.candidate.bar_end + ttl, self.mono()))
        self._why_not[m.spec.name] = (f"{rs.regime}; best {res.best.candidate.strategy} {res.best.direction} "
                                      f"{res.best.score:.0f}" if res.best else f"{rs.regime}; no eligible setup")

    def _try_enter(self, now: dt.datetime) -> None:
        self.pending = [p for p in self.pending if p.expires > now]
        if not self.pending or not self.reconciled:
            return
        self.pending.sort(key=lambda p: -p.signal.score)
        p = self.pending[0]
        sig = p.signal
        c = sig.candidate
        m = self.markets[c.index]
        spot = m.spot()
        if spot is None:
            return                                       # stale underlying: retry until the signal expires
        res = select_contract(c.direction, spot, m.step, m.contracts, m.quotes(), self.lots_fn(c.index),
                              self.max_lots, self.limits, self.costs, self.s.strategy.contracts, now.date(),
                              self.mono(), now, self.s.data.quote_stale_s)
        sel = res.selection
        if sel is None:
            transient = res.rejections and all(any(t in why for t in TRANSIENT) for _s, why in res.rejections)
            if transient:
                return                                   # quotes still arriving: retry until the TTL
            self._drop(p, now, "no contract passed: " + "; ".join(f"{s} {w}" for s, w in res.rejections[:4]), res)
            return
        decision, total = self.ens.final_decision(sig, sel.liquidity_pts)
        if decision != ELIGIBLE:
            self._drop(p, now, f"final score {total:.0f} with option liquidity: {decision}", res)
            return
        q = sel.quote
        cp = self.s.strategy.contracts
        ctx = EntryContext(now=now, has_position=self.position is not None,
                           pending_orders=len(self.store.open_orders(self.mode)), spot_fresh=True,
                           quote_problem=q.problem(self.mono(), self.s.data.quote_stale_s, now),
                           instruments_day=m.instruments_day, lots=sel.lots, lot_size=sel.contract.lot_size,
                           qty=sel.qty, outlay=sel.outlay,
                           spread_ok=sel.spread_pct <= cp.max_spread_pct and (q.volume or 0) >= cp.min_volume,
                           feasible=(sel.ask - sel.bid) * sel.qty + sel.entry_charges + sel.exit_charges_est
                           <= self.limits.emergency_feasibility_frac * self.limits.emergency_loss_per_trade,
                           reconciled=self.reconciled, feed_ok=m.data_ok)
        ok, why = approve_entry(ctx, self.book, self.limits, self.s.session)
        self.store.selection(now, c.index, c.setup_key, sel, res.rejections)
        if not ok:
            self._drop(p, now, "risk: " + "; ".join(why), None, level="INFO")
            return
        self.pending.remove(p)
        self._enter(sig, sel, now, total)

    def _drop(self, p: Pending, now: dt.datetime, why: str, res, level: str = "INFO") -> None:
        c = p.signal.candidate
        if p in self.pending:
            self.pending.remove(p)
        self.store.consume(now.date(), c.setup_key, why[:200])
        if res is not None:
            self.store.selection(now, c.index, c.setup_key, None, res.rejections)
        log.info("SIGNAL %s %s %s not traded: %s", c.index, c.strategy, c.direction, why)
        self.n.alert(f"Signal {c.index} {c.strategy} {c.direction} (score {p.signal.score:.0f}) not traded: {why}"[:600],
                     level, f"drop-{c.setup_key}")

    # ------------------------------------------------------------------ entry
    def _enter(self, sig: Signal, sel, now: dt.datetime, total: float) -> None:
        c = sig.candidate
        m = self.markets[c.index]
        today = now.date()
        pid = f"{today:%Y%m%d}-{c.index}-{self.store.next_order_seq(self.exe.prefix + today.strftime('%y%m%d'))}"
        pos = Position(pid, c.index, sel.contract, sel.lots, 0, sel.ask, now, c.strategy, c.direction, c.setup_key,
                       total, c.invalidation, m.spot() or c.ref_price, sel.spread_pct, 0.0, tuple(sig.reasons),
                       status="PENDING")
        self.store.save_position(self.mode, pos)
        self.store.consume(today, c.setup_key, "entered")
        ex = self.exe.execute(kind="entry", position_id=pid, index=c.index, token=sel.contract.token,
                              exchange=sel.contract.exchange, symbol=sel.contract.symbol, side="BUY", qty=sel.qty,
                              lot_size=sel.contract.lot_size, tick_size=sel.contract.tick_size,
                              reason=f"{c.strategy} {c.direction} score {total:.0f}", today=today)
        ts = now.isoformat(timespec="seconds")
        if ex.state == UNKNOWN and ex.filled == 0:
            self.book.latch("entry order state unknown", now)
            pos.status = "PENDING"
            self.store.save_position(self.mode, pos)
            self.position = pos
            self.n.alert(f"ENTRY order {ex.client_id} state UNKNOWN for {sel.contract.symbol}: kill switch latched. "
                         "Check Kite NOW.", "CRITICAL", "entry-unknown")
            return
        if ex.filled <= 0:
            pos.status = "VOID"
            self.store.save_position(self.mode, pos, exit_reason=f"entry {ex.state}: {ex.detail}")
            self.entry_failures += 1
            if self.entry_failures >= self.MAX_ENTRY_FAILURES:
                self.book.day_block = f"{self.entry_failures} failed entries today"
            self.n.alert(f"ENTRY {c.index} {sel.contract.symbol} not filled ({ex.state}: {ex.detail})", "WARNING",
                         f"entry-fail-{ex.client_id}")
            self._save()
            return
        cp = self.costs.for_exchange(sel.contract.exchange)
        pos.qty = ex.filled
        pos.lots = ex.filled // sel.contract.lot_size
        pos.entry_price = ex.avg_price
        pos.entry_charges = round(trade_cost([("BUY", ex.avg_price, ex.filled)], cp), 2)
        pos.status = "OPEN"
        self.position = pos
        self.tracker = OptionTracker()
        self.emergency.reset()
        self.book.on_entry()
        self.store.save_position(self.mode, pos)
        self._save()
        payload = {"index": c.index, "lots": pos.lots, "symbol": sel.contract.symbol, "qty": pos.qty,
                   "fill": ex.avg_price, "ask": sel.ask, "bid": sel.bid, "spread_pct": sel.spread_pct,
                   "score": total, "components": sig.components, "regime": sig.regime, "spot": pos.entry_spot,
                   "invalidation": c.invalidation, "rationale": f"[{c.strategy}] " + "; ".join(sig.reasons)[:300],
                   "requested_lots": sel.lots, "client_id": ex.client_id}
        self.n.journal_trade(ts, variant_of(pos), "entry", "", 0.0, 0.0, 0.0, payload)
        self.n.order_row([ts, self.mode, variant_of(pos), "ENTRY", sel.contract.symbol, "BUY", ex.filled,
                          round(sel.ask, 2), round(ex.avg_price, 2), f"{c.strategy} score {total:.0f}"])
        part = f" (partial: {ex.filled}/{sel.qty})" if ex.filled < sel.qty else ""
        self.n.alert(f"ENTRY {self.mode} {c.index} BUY {sel.contract.symbol} x{ex.filled}{part} @ {ex.avg_price:.2f} | "
                     f"{c.strategy} {c.direction} score {total:.0f} ({sig.regime}) | invalidation {c.invalidation:.2f} "
                     f"| outlay {ex.avg_price * ex.filled + pos.entry_charges:,.0f}")

    # ------------------------------------------------------------------ position management
    def _quote_for(self, m: IndexMarket, pos: Position) -> tuple[Optional[OptionQuote], Optional[str]]:
        q = m.quote(pos.contract.token)
        prob = q.problem(self.mono(), self.s.data.quote_stale_s) if q else "no quote"
        if prob and self.mono() - self._last_rest >= self.REST_QUOTE_EVERY_S:
            self._last_rest = self.mono()
            try:
                q2 = m.rest_quote(pos.contract.token)
                if q2 is not None and not q2.problem(self.mono(), self.s.data.quote_stale_s):
                    return q2, None
            except Exception:  # noqa: BLE001
                log.warning("REST quote failed for %s", pos.contract.symbol, exc_info=True)
        return q, prob

    def _manage(self, now: dt.datetime) -> None:
        pos = self.position
        m = self.markets[pos.index]
        if pos.status == "PENDING":
            return                                       # unknown entry state: kill switch latched, human checks
        q, prob = self._quote_for(m, pos)
        cp = self.costs.for_exchange(pos.contract.exchange)
        unreal = None
        if q is not None and q.bid and not prob:
            unreal = pos.unrealized(q.bid, trade_cost([("SELL", q.bid, pos.qty)], cp))
            pos.last_bid = q.bid
            pos.peak_pnl = max(pos.peak_pnl, unreal)
            pos.trough_pnl = min(pos.trough_pnl, unreal)
            premium = pos.entry_price * (pos.qty + pos.closed_qty)
            if not pos.milestone_hit and premium > 0 and \
                    pos.peak_pnl >= self.s.strategy.exits.profit_milestone_premium_pct / 100 * premium:
                pos.milestone_hit = True
                log.info("Profit milestone reached on %s (peak %.0f)", pos.contract.symbol, pos.peak_pnl)
        spot = m.spot()
        if spot is not None:
            mv = (spot - pos.entry_spot) * (1 if pos.direction == "CE" else -1)
            pos.peak_spot_move = max(pos.peak_spot_move, mv)
        if self.mode == "LIVE" and self.broker_positions_fn is not None and \
                self.mono() - self._last_broker_check >= self.BROKER_CHECK_S:
            self._last_broker_check = self.mono()
            try:
                held = self.broker_positions_fn().get(pos.contract.symbol, 0)
                self._broker_ok = held == pos.qty
            except Exception:  # noqa: BLE001
                log.warning("Broker position check failed", exc_info=True)
        trig = self.emergency.check(pos, q, unreal, self.book, now, self.mono(), broker_ok=self._broker_ok,
                                    reconciled=self.reconciled, quote_problem=prob)
        if trig is not None:
            self._emergency(trig, now)
            return
        f = m.frame
        i = f.last if f is not None else None
        new_bar = m.consume_new_bar()
        if new_bar and f is not None:
            lvl = update_structure(pos, f, i, int(self.s.strategy.exits.structure_trail_bars))
            if lvl is not None:
                log.info("Structure trail %s: invalidation %.2f -> %.2f", pos.contract.symbol, pos.invalidation, lvl)
                pos.invalidation = lvl
        self.tracker.update(q if not prob else None, now)
        a = self.exits.assess(pos, f, i, self.tracker, spot, now, new_bar)
        if new_bar or self.last_assessment is None or a.action != self.last_assessment.action:
            self.store.exit_assessment(pos.id, a)
            log.info("EXIT-ASSESS %s total %.0f %s %s | %s", pos.contract.symbol, a.total, a.action,
                     {k: round(v, 1) for k, v in a.components.items()}, "; ".join(a.reasons))
        self.last_assessment = a
        if a.action == EXIT and self.mono() >= self._next_exit_try:
            self._close(pos.qty, f"ADAPTIVE_EXIT {a.total:.0f}" + (" STRUCTURE" if a.structural else "") +
                        (": " + "; ".join(a.reasons) if a.reasons else ""), now, emergency=False)
        elif a.action == REDUCE and self.mono() >= self._next_exit_try:
            self._close(pos.contract.lot_size, f"REDUCE 1 lot (profit protection, score {a.total:.0f})", now,
                        emergency=False)
        if self.mono() - self._last_save > 15:
            self.store.save_position(self.mode, pos)

    def _emergency(self, trig: EmergencyTrigger, now: dt.datetime) -> None:
        pos = self.position
        first = pos.status != "CLOSING"
        if first:
            self.store.risk_event(self.mode, trig.kind, {"detail": trig.detail, "symbol": pos.contract.symbol,
                                                         "qty": pos.qty, "latch": trig.latch})
            if trig.latch:
                self.book.latch(f"{trig.kind}: {trig.detail}", now)
            if trig.kind == "daily_loss" and not self.book.day_block:
                self.book.day_block = f"daily loss limit: {trig.detail}"
            lvl = "INFO" if trig.kind == "session_close" else "CRITICAL"
            self.n.alert(f"{'SQUARE-OFF' if trig.kind == 'session_close' else 'EMERGENCY'} {trig.kind}: {trig.detail}. "
                         f"Closing {pos.contract.symbol} x{pos.qty}." + (" Kill switch latched (/resetkill after review)."
                                                                         if trig.latch else ""), lvl,
                         f"emerg-{pos.id}-{trig.kind}")
        if self.mono() >= self._next_exit_try:
            self._close(pos.qty, trig.kind.upper(), now, emergency=True)

    def _close(self, qty: int, reason: str, now: dt.datetime, emergency: bool) -> None:
        pos = self.position
        qty = min(qty, pos.qty)
        if qty <= 0:
            return
        pos.status = "CLOSING"
        self.store.save_position(self.mode, pos)
        lv = self.s.execution.live
        market = self.mode == "LIVE" and pos.exit_attempts >= lv.exit_market_after_failures
        q = self.markets[pos.index].quote(pos.contract.token)
        ex = self.exe.execute(kind="exit", position_id=pos.id, index=pos.index, token=pos.contract.token,
                              exchange=pos.contract.exchange, symbol=pos.contract.symbol, side="SELL", qty=qty,
                              lot_size=pos.contract.lot_size, tick_size=pos.contract.tick_size, reason=reason[:120],
                              emergency=emergency, market=market, today=now.date())
        ts = now.isoformat(timespec="seconds")
        if ex.filled > 0:
            cp = self.costs.for_exchange(pos.contract.exchange)
            initial = pos.qty + pos.closed_qty
            gross = (ex.avg_price - pos.entry_price) * ex.filled
            cost = trade_cost([("SELL", ex.avg_price, ex.filled)], cp) + pos.entry_charges * ex.filled / initial
            pos.closed_gross += gross
            pos.closed_cost += cost
            pos.closed_qty += ex.filled
            pos.qty -= ex.filled
            pos.lots = pos.qty // pos.contract.lot_size
            self.book.add_realized(gross - cost, pos.id)
            self.n.order_row([ts, self.mode, variant_of(pos), "EXIT" if pos.qty == 0 else "EXIT-PARTIAL",
                              pos.contract.symbol, "SELL", ex.filled, round(q.bid, 2) if q and q.bid else None,
                              round(ex.avg_price, 2), reason[:120]])
        if pos.qty == 0:
            self._finalise(pos, reason, now)
            return
        if ex.filled < qty:
            pos.exit_attempts += 1
            self._next_exit_try = self.mono() + self.EXIT_RETRY_S
            if ex.state == UNKNOWN:
                self.book.latch(f"exit order {ex.client_id} state unknown", now)
            if pos.exit_attempts >= lv.max_exit_attempts:
                self.book.latch(f"exit not completing after {pos.exit_attempts} attempts", now)
                self.n.alert(f"EXIT NOT COMPLETING for {pos.contract.symbol}: {pos.qty} still open after "
                             f"{pos.exit_attempts} attempts. Close it in Kite NOW. Kill switch latched.", "CRITICAL",
                             f"exit-stuck-{pos.id}")
            else:
                self.n.alert(f"Exit of {pos.contract.symbol} incomplete ({ex.state}, filled {ex.filled}/{qty}); "
                             f"retrying (attempt {pos.exit_attempts}).", "WARNING" if not emergency else "CRITICAL",
                             f"exit-retry-{pos.id}-{pos.exit_attempts}")
        else:
            pos.status = "OPEN"                         # a partial REDUCE that filled: keep managing the rest
            pos.exit_attempts = 0
        self.store.save_position(self.mode, pos)
        self._save()

    def _finalise(self, pos: Position, reason: str, now: dt.datetime) -> None:
        pos.status = "CLOSED"
        net = round(pos.closed_gross - pos.closed_cost, 2)
        self.store.save_position(self.mode, pos, gross=round(pos.closed_gross, 2), cost=round(pos.closed_cost, 2),
                                 net=net, exit_reason=reason[:200])
        self.book.on_close(net, now, self.limits)
        self.book.booked.pop(pos.id, None)               # fully booked: nothing left to reconcile for it
        ts = now.isoformat(timespec="seconds")
        self.n.journal_trade(ts, variant_of(pos), "exit", reason[:200], round(pos.closed_gross, 2),
                             round(pos.closed_cost, 2), net,
                             {"index": pos.index, "lots": pos.closed_qty // pos.contract.lot_size,
                              "peak_pnl": round(pos.peak_pnl, 2), "trough_pnl": round(pos.trough_pnl, 2),
                              "giveback": round(pos.peak_pnl - net, 2) if pos.peak_pnl > 0 else 0.0,
                              "symbol": pos.contract.symbol, "day_pnl": round(self.book.day_realized, 2)})
        self.n.trade_row([ts, self.mode, variant_of(pos), reason[:120], pos.closed_qty // pos.contract.lot_size,
                          round(pos.closed_gross, 2), round(pos.closed_cost, 2), net, round(self.book.day_realized, 2)])
        give = f", gave back {pos.peak_pnl - net:,.0f} from peak {pos.peak_pnl:,.0f}" if pos.peak_pnl > 0 else ""
        self.n.alert(f"EXIT {self.mode} {pos.index} {pos.contract.symbol} [{reason[:160]}] net {net:,.0f}{give}; "
                     f"day {self.book.day_realized:,.0f}")
        if self.position is not None and self.position.id == pos.id:
            self.position = None
        self.tracker = OptionTracker()
        self.last_assessment = None
        self.emergency.reset()
        self._save()

    def end_of_session(self, now: dt.datetime) -> None:
        """After the close. A paper position still open (no quotes reached its exit) is marked at its last bid; a live
        one is flagged (MIS: the broker squares it off)."""
        pos = self.position
        if pos is None:
            return
        if self.mode == "PAPER":
            self._close_stale(pos, now.date() + dt.timedelta(days=1))
            self.position = None
        else:
            self.n.alert(f"LIVE position {pos.contract.symbol} x{pos.qty} still open after the close: verify the "
                         "broker's MIS square-off in Kite.", "CRITICAL", "eod-open")

    # ------------------------------------------------------------------ control / state
    def kill(self, reason: str) -> str:
        self.book.latch(reason, dt.datetime.now(IST))
        self.store.risk_event(self.mode, "manual_kill", {"reason": reason})
        self._save()
        return f"Kill switch latched ({reason}). No new entries. An open position is still managed."

    def reset_kill(self) -> str:
        was = self.book.reset_kill()
        self.store.risk_event(self.mode, "kill_reset", {"was": was})
        self._save()
        return f"Kill switch reset (was: {was or 'not latched'})."

    def _save(self) -> None:
        self._last_save = self.mono()
        try:
            self.store.set_state(self.mode, "risk_book", self.book.export())
            if self.position is not None:
                self.store.save_position(self.mode, self.position)
        except Exception:  # noqa: BLE001
            log.critical("Could not persist the risk book", exc_info=True)
            self.n.alert("Risk state write FAILED (disk?): fix the VM now.", "CRITICAL", "state-write")
        if self.state_file:
            self._write_state_file()

    def _write_state_file(self) -> None:
        """data/state.json for the VM's autostop guard (deploy/can_stop.py): `position` is non-null while open."""
        pos = self.position
        doc = {"date": dt.datetime.now(IST).date().isoformat(), "mode": self.mode,
               "position": None if pos is None else {"plan": {"name": f"{pos.index} {pos.contract.symbol}"},
                                                     "qty": pos.qty, "status": pos.status}}
        try:
            d = os.path.dirname(os.path.abspath(self.state_file))
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, self.state_file)
        except OSError:
            log.critical("Could not write %s", self.state_file, exc_info=True)

    def status(self) -> dict:
        pos = self.position
        a = self.last_assessment
        L = self.limits
        return {
            "mode": self.mode, "reconciled": self.reconciled,
            "kill_switch": {"latched": self.book.kill_latched, "reason": self.book.kill_reason, "at": self.book.kill_at},
            "day_block": self.book.day_block,
            "book": {"day": self.book.day, "day_pnl": round(self.book.day_realized, 2),
                     "week_pnl": round(self.book.week_realized, 2), "entries_today": self.book.entries_today,
                     "consecutive_losses": self.book.consecutive_losses, "equity": round(self.book.equity, 2),
                     "peak_equity": round(self.book.peak_equity, 2)},
            "limits": {"scale": round(L.scale, 3), "premium_cap": round(L.max_premium_per_entry),
                       "emergency_per_trade": round(L.emergency_loss_per_trade), "daily": round(L.daily_loss_limit),
                       "weekly": round(L.weekly_loss_limit), "drawdown": round(L.max_drawdown),
                       "max_entries": L.max_entries_per_day},
            "position": None if pos is None else {
                "index": pos.index, "symbol": pos.contract.symbol, "qty": pos.qty, "entry": round(pos.entry_price, 2),
                "since": pos.entry_time.isoformat(timespec="seconds"), "strategy": pos.strategy,
                "direction": pos.direction, "invalidation": round(pos.invalidation, 2),
                "peak_pnl": round(pos.peak_pnl, 2), "status": pos.status,
                "exit_score": None if a is None else a.total, "exit_action": None if a is None else a.action,
                "exit_components": None if a is None else a.components},
            "indices": {name: {"data_ok": m.data_ok, "why": m.data_why, "spot": m.spot(),
                               "regime": self.regimes.get(name).regime if name in self.regimes else None,
                               "last": self._why_not.get(name, "waiting for a completed bar"),
                               "future_volume": m.future_token is not None}
                        for name, m in self.markets.items()},
            "pending_signals": [{"index": p.signal.index, "strategy": p.signal.candidate.strategy,
                                 "direction": p.signal.direction, "score": p.signal.score,
                                 "expires": p.expires.isoformat(timespec="seconds")} for p in self.pending],
        }
