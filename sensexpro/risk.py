"""Risk manager (brief section 6): persistent risk book, circuit breakers, kill switch and pre-trade approval.

Every entry intent is approved here after signal generation and contract selection; strategies never reach the
broker. All counters live in SQLite (Store.sp_state), so a restart never resets them.

Distinctions kept explicit (brief 6):
  * premium at risk   = what was paid for the option (a long option can lose all of it);
  * mark-to-market    = position value at the BID minus estimated exit charges, minus entry charges;
  * emergency budget  = RiskLimits.emergency_loss_per_trade (a threshold, NOT a guaranteed maximum loss);
  * account breakers  = daily / weekly / drawdown on realised + unrealised P&L.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

from .settings import RiskLimits, Session

log = logging.getLogger(__name__)


def week_key(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


@dataclass
class RiskBook:
    capital_base: float = 0.0             # equity reference (paper: PAPER_CAPITAL; live: cash at first session)
    cum_realized: float = 0.0             # since the book was created
    peak_equity: float = 0.0
    day: Optional[str] = None
    day_realized: float = 0.0
    entries_today: int = 0
    consecutive_losses: int = 0
    week: Optional[str] = None
    week_realized: float = 0.0
    last_exit_at: Optional[str] = None    # ISO time of the last exit (cool-down)
    day_block: str = ""                   # entries blocked for the rest of the day (daily loss / loss streak)
    kill_latched: bool = False
    kill_reason: str = ""
    kill_at: Optional[str] = None
    booked: dict = field(default_factory=dict)   # position id -> realised P&L already added (restart-safe)

    # ------------------------------------------------------------------ lifecycle
    def ensure_base(self, capital: float) -> None:
        if self.capital_base <= 0 and capital > 0:
            self.capital_base = capital
            self.peak_equity = max(self.peak_equity, capital)

    def roll(self, today: dt.date) -> bool:
        """New day / week resets (never resets the kill switch or the equity peak). True if the day changed."""
        changed = False
        if self.day != today.isoformat():
            self.day, self.day_realized, self.entries_today = today.isoformat(), 0.0, 0
            self.consecutive_losses, self.day_block = 0, ""
            changed = True
        wk = week_key(today)
        if self.week != wk:
            self.week, self.week_realized = wk, 0.0
        return changed

    @property
    def equity(self) -> float:
        return self.capital_base + self.cum_realized

    def on_entry(self) -> None:
        self.entries_today += 1

    def add_realized(self, net: float, position_id: Optional[str] = None) -> None:
        """Realised P&L of a (partial) exit, counted at once for every breaker."""
        if position_id:
            self.booked[position_id] = self.booked.get(position_id, 0.0) + net
        self.cum_realized += net
        self.day_realized += net
        self.week_realized += net
        self.peak_equity = max(self.peak_equity, self.equity)

    def on_close(self, trade_net: float, at: dt.datetime, limits: RiskLimits) -> None:
        """The whole trade is closed (its realised P&L was already added with add_realized)."""
        self.last_exit_at = at.isoformat()
        self.consecutive_losses = self.consecutive_losses + 1 if trade_net < 0 else 0
        if self.consecutive_losses >= limits.max_consecutive_losses and not self.day_block:
            self.day_block = f"{self.consecutive_losses} consecutive losses: no new entries today (review)"

    def latch(self, reason: str, at: dt.datetime) -> None:
        if not self.kill_latched:
            self.kill_latched, self.kill_reason, self.kill_at = True, reason, at.isoformat()

    def reset_kill(self) -> str:
        was = self.kill_reason
        self.kill_latched, self.kill_reason, self.kill_at = False, "", None
        return was

    def export(self) -> dict:
        return asdict(self)

    @classmethod
    def restore(cls, d: Optional[dict]) -> "RiskBook":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Breach:
    kind: str                             # daily_loss | weekly_loss | drawdown
    detail: str


def breaker_breach(book: RiskBook, limits: RiskLimits, unrealized: float = 0.0) -> Optional[Breach]:
    """Account-level breakers on realised + current unrealised P&L (no double counting: unrealized is the open
    position only, realised P&L is already in the book)."""
    day = book.day_realized + unrealized
    if day <= -limits.daily_loss_limit:
        return Breach("daily_loss", f"day P&L {day:,.0f} <= -{limits.daily_loss_limit:,.0f}")
    week = book.week_realized + unrealized
    if week <= -limits.weekly_loss_limit:
        return Breach("weekly_loss", f"week P&L {week:,.0f} <= -{limits.weekly_loss_limit:,.0f}")
    dd = book.peak_equity - (book.equity + unrealized)
    if book.peak_equity > 0 and dd >= limits.max_drawdown:
        return Breach("drawdown", f"drawdown {dd:,.0f} >= {limits.max_drawdown:,.0f}")
    return None


@dataclass
class EntryContext:
    """Everything the pre-trade check needs; built by the trading loop right before an order intent."""
    now: dt.datetime
    has_position: bool
    pending_orders: int
    spot_fresh: bool
    quote_problem: Optional[str]
    instruments_day: Optional[dt.date]
    lots: int
    lot_size: int
    qty: int
    outlay: float
    spread_ok: bool
    feasible: bool
    reconciled: bool
    feed_ok: bool
    notes: list = field(default_factory=list)


def approve_entry(ctx: EntryContext, book: RiskBook, limits: RiskLimits, session: Session) -> tuple[bool, list[str]]:
    """(approved, reasons). Every failed check is listed (fail closed)."""
    why: list[str] = []
    t = ctx.now.time()
    if ctx.now.weekday() >= 5:
        why.append("weekend")
    if not session.entry_start <= t <= session.entry_end:
        why.append(f"outside the entry window {session.entry_start:%H:%M}-{session.entry_end:%H:%M}")
    if book.kill_latched:
        why.append(f"kill switch latched ({book.kill_reason})")
    if book.day_block:
        why.append(book.day_block)
    b = breaker_breach(book, limits)
    if b:
        why.append(f"circuit breaker: {b.detail}")
    if ctx.has_position:
        why.append("a position is already open (max 1)")
    if ctx.pending_orders:
        why.append(f"{ctx.pending_orders} unresolved order(s)")
    if book.entries_today >= limits.max_entries_per_day:
        why.append(f"{book.entries_today} entries today (max {limits.max_entries_per_day})")
    if book.last_exit_at and limits.cooldown_after_exit_min > 0:
        since = (ctx.now - dt.datetime.fromisoformat(book.last_exit_at)).total_seconds() / 60
        if since < limits.cooldown_after_exit_min:
            why.append(f"cool-down after exit ({since:.0f} of {limits.cooldown_after_exit_min:.0f} min)")
    if not ctx.reconciled:
        why.append("broker state not reconciled yet")
    if not ctx.feed_ok:
        why.append("market feed not healthy")
    if not ctx.spot_fresh:
        why.append("underlying price stale")
    if ctx.quote_problem:
        why.append(f"option quote: {ctx.quote_problem}")
    if ctx.instruments_day != ctx.now.date():
        why.append("instrument master not loaded today")
    if ctx.lots < 1 or ctx.lot_size < 1 or ctx.qty != ctx.lots * ctx.lot_size:
        why.append("quantity is not a whole number of lots")
    if ctx.outlay > limits.max_premium_per_entry:
        why.append(f"outlay {ctx.outlay:,.0f} > cap {limits.max_premium_per_entry:,.0f}")
    if not ctx.spread_ok:
        why.append("spread / volume / depth check failed")
    if not ctx.feasible:
        why.append("emergency budget not feasible for this contract")
    return (not why), why
