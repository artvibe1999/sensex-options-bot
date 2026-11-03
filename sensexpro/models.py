"""Core data contracts (brief section 2.1).

In memory, datetimes are timezone-aware (IST for session logic); everything persisted is converted to UTC ISO-8601.
Quotes carry both the exchange timestamp and the local receive time; LTP is never treated as an executable price when
bid/ask exist.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = dt.timezone.utc
CE, PE = "CE", "PE"


def utc_iso(t: Optional[dt.datetime]) -> Optional[str]:
    if t is None:
        return None
    if t.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must be timezone-aware")
    return t.astimezone(UTC).isoformat(timespec="seconds")


def from_utc_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError(f"stored timestamp without a timezone: {s}")
    return t.astimezone(IST)


def _finite_pos(x: Optional[float]) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


# ---------------------------------------------------------------- candles
@dataclass(frozen=True)
class Bar:
    """One OHLCV candle of the underlying. `start` is aware (IST); volume is the index future's volume (indices have
    none of their own), None when unavailable."""
    start: dt.datetime
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    gap: bool = False                     # aggregated from fewer 1-minute candles than it should contain

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("Bar.start must be timezone-aware")
        if not all(_finite_pos(v) for v in (self.open, self.high, self.low, self.close)):
            raise ValueError(f"Bar prices must be positive and finite: {self}")
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"Bar high/low do not bracket open/close: {self}")
        if self.volume is not None and (not math.isfinite(self.volume) or self.volume < 0):
            raise ValueError("Bar volume must be >= 0")

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(minutes=self.minutes)

    @property
    def range(self) -> float:
        return self.high - self.low


# ---------------------------------------------------------------- contracts and quotes
@dataclass(frozen=True)
class ContractMeta:
    """From the broker's current instrument master; never hardcoded."""
    index: str
    exchange: str
    token: int
    symbol: str
    expiry: dt.date
    strike: float
    kind: str                     # CE | PE
    lot_size: int
    tick_size: float
    freeze_qty: Optional[int] = None

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class OptionQuote:
    contract: ContractMeta
    bid: Optional[float]
    ask: Optional[float]
    bid_qty: Optional[int]
    ask_qty: Optional[int]
    ltp: Optional[float]
    volume: Optional[float]
    oi: Optional[float]
    avg_price: Optional[float]            # the exchange's day VWAP for the contract (FULL-mode tick)
    exchange_ts: Optional[dt.datetime]
    recv_mono: float                      # local monotonic receive time
    recv_at: dt.datetime                  # local wall-clock receive time (aware)

    @property
    def mid(self) -> Optional[float]:
        if _finite_pos(self.bid) and _finite_pos(self.ask):
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        return (self.ask - self.bid) / m * 100 if m else None

    def problem(self, now_mono: float, stale_s: float, now: Optional[dt.datetime] = None) -> Optional[str]:
        """None if the quote is executable now, else the reason it is not (stale, crossed, zero, missing side)."""
        if not _finite_pos(self.ask):
            return "no ask"
        if not _finite_pos(self.bid):
            return "no bid"
        if self.ask < self.bid:
            return f"crossed (bid {self.bid} > ask {self.ask})"
        age = now_mono - self.recv_mono
        if age > stale_s:
            return f"stale ({age:.1f}s since receipt)"
        if now is not None and self.exchange_ts is not None:
            ex_age = (now - self.exchange_ts).total_seconds()
            if ex_age > max(stale_s, 1.0) * 6:          # exchange time far behind: book not updating
                return f"stale exchange time ({ex_age:.0f}s)"
        return None


# ---------------------------------------------------------------- signals and exits
@dataclass(frozen=True)
class Candidate:
    """What a strategy emits: a setup, never an order."""
    index: str
    strategy: str
    direction: str                         # CE (bullish underlying) | PE (bearish)
    bar_end: dt.datetime
    setup_key: str                         # identifies the setup: the same key is never traded twice
    price_action: float                    # 0..1 strength of the strategy's own confirmation
    ref_price: float                       # underlying close of the signal bar
    invalidation: float                    # underlying level that proves the thesis wrong
    reasons: tuple = ()
    evidence: dict = field(default_factory=dict)
    strong: bool = False                   # strategy-specific "strong" evidence (failed breakouts in a trend)


@dataclass(frozen=True)
class Signal:
    candidate: Candidate
    regime: str
    score: float
    components: dict
    decision: str                          # ELIGIBLE | WATCHLIST | REJECT
    reasons: tuple = ()

    @property
    def index(self) -> str:
        return self.candidate.index

    @property
    def direction(self) -> str:
        return self.candidate.direction


@dataclass(frozen=True)
class ExitAssessment:
    at: dt.datetime
    components: dict                       # name -> points scored (0..weight)
    missing: tuple                         # inputs that were unavailable (scored at missing_input_share)
    total: float
    action: str                            # HOLD | REASSESS | PREPARE_EXIT | EXIT | REDUCE
    reasons: tuple
    structural: bool = False               # confirmed structure invalidation (immediate exit)
    emergency: bool = False


# ---------------------------------------------------------------- positions
@dataclass
class Position:
    id: str
    index: str
    contract: ContractMeta
    lots: int
    qty: int                               # currently held quantity (whole lots)
    entry_price: float                     # average fill
    entry_time: dt.datetime
    strategy: str
    direction: str
    setup_key: str
    score: float
    invalidation: float                    # current structural level (trails confirmed swings)
    entry_spot: float
    entry_spread_pct: float
    entry_charges: float
    reasons: tuple = ()
    peak_pnl: float = 0.0
    trough_pnl: float = 0.0
    peak_spot_move: float = 0.0            # best underlying move in the trade's direction (points)
    prepare_count: int = 0
    last_bar_seen: Optional[str] = None
    milestone_hit: bool = False
    closed_gross: float = 0.0              # partial exits so far
    closed_cost: float = 0.0
    closed_qty: int = 0
    exit_attempts: int = 0
    last_bid: Optional[float] = None       # last executable bid seen (marks a position the session lost track of)
    status: str = "OPEN"                   # PENDING (entry in flight) | OPEN | CLOSING | CLOSED | VOID (never filled)

    def unrealized(self, bid: float, exit_cost: float) -> float:
        """Mark-to-market at the executable price (bid) after estimated exit charges; entry charges included."""
        open_part = (bid - self.entry_price) * self.qty - exit_cost
        entry_share = self.entry_charges * self.qty / max(self.qty + self.closed_qty, 1)
        return open_part - entry_share

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("contract", "entry_time", "reasons")}
        c = self.contract
        d["contract"] = {"index": c.index, "exchange": c.exchange, "token": c.token, "symbol": c.symbol,
                         "expiry": c.expiry.isoformat(), "strike": c.strike, "kind": c.kind,
                         "lot_size": c.lot_size, "tick_size": c.tick_size, "freeze_qty": c.freeze_qty}
        d["entry_time"] = utc_iso(self.entry_time)
        d["reasons"] = list(self.reasons)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d = dict(d)
        c = dict(d.pop("contract"))
        c["expiry"] = dt.date.fromisoformat(c["expiry"])
        d["contract"] = ContractMeta(**c)
        d["entry_time"] = from_utc_iso(d["entry_time"])
        d["reasons"] = tuple(d.get("reasons") or ())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
