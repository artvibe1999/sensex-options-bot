"""Trend / range playbook (researched 2026-10-31 on SENSEX 1-minute history 2018-03 .. 2026-10, see README).

SINGLE LEG (strong trend) - takes priority:
  entry  : a 15-minute bar closes with Supertrend(10,2) FLIPPING and Supertrend(10,3) already pointing the same way;
           bar end between 10:30 and 13:00; India VIX >= 14. Buy a CE (up) / PE (down), at most 2 per day.
  strike : chosen against time decay: >= 3 trading days to expiry -> ATM; 2 days -> 200 pts ITM; <= 1 day -> 400 ITM.
  exit   : NO stop-loss and NO trailing stop. Hold while the trend continues and square off at 15:15. Exit early only
           on a trend REVERSAL (Supertrend(10,2) of the last completed 15-minute bar turns against the trade) or if the
           trade is Rs 1,500 down (LOSS_CAP_RS).
MULTI LEG (high VIX, no trend):
  entry  : first moment between 10:00 and 12:00 with VIX >= 14 and 15-minute ADX(14) < 25, if no single-leg trade was
           taken today. Iron condor: sell the CE/PE about 0.8% out of the money, buy wings about 1.7% further out.
  exit   : 15:15, or Rs 1,500 down, or a single-leg trend signal fires (the condor is closed and the trend trade taken).
Lots: single 2 and multi 2 by default; change them from Telegram (/lots, /mlots).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from pb_indicators import Candle, adx, supertrend, to_15m
from strategy_engine import Chain, Leg, TradePlan

PREFIX = "pb_"


@dataclass(frozen=True)
class PlaybookParams:
    single_t0: dt.time = dt.time(10, 30)     # signal bar END time window
    single_t1: dt.time = dt.time(13, 0)
    single_min_vix: float = 14.0
    single_max_per_day: int = 2
    multi_t0: dt.time = dt.time(10, 0)
    multi_t1: dt.time = dt.time(12, 0)
    multi_min_vix: float = 14.0
    multi_max_adx: float = 25.0
    condor_short_pct: float = 0.008
    condor_wing_pct: float = 0.017
    loss_cap_rs: float = 1500.0              # applies to single and multi leg; 0 = off
    itm_step_pts: float = 100.0              # strike step (the chain's own step is used when known)

    @classmethod
    def from_env(cls) -> "PlaybookParams":
        import os
        return cls(loss_cap_rs=float(os.getenv("PB_LOSS_CAP_RS", "1500")),
                   single_min_vix=float(os.getenv("PB_SINGLE_MIN_VIX", "14")),
                   multi_min_vix=float(os.getenv("PB_MULTI_MIN_VIX", "14")))


@dataclass(frozen=True)
class TrendState:
    """Indicators on the last COMPLETED 15-minute bar."""
    bar_end: dt.datetime
    st2: int                  # Supertrend(10,2) direction
    st2_prev: int
    st3: int                  # Supertrend(10,3) direction
    adx: float

    @property
    def flipped(self) -> bool:
        return self.st2 != self.st2_prev

    @property
    def signal(self) -> int:
        """+1 / -1 on a confirmed trend-start bar, else 0."""
        return self.st2 if (self.flipped and self.st3 == self.st2) else 0


def trend_state(bars5: Sequence[Candle], now: dt.datetime) -> Optional[TrendState]:
    """15-minute Supertrend(10,2), Supertrend(10,3) and ADX(14) from the completed 5-minute bars (several days of them,
    so the indicators are warmed up). Only 15-minute bars that have fully closed by `now` are used."""
    done5 = [b for b in bars5 if b.ts + dt.timedelta(minutes=5) <= now]
    b15 = [b for b in to_15m(done5) if b.ts + dt.timedelta(minutes=15) <= now]
    # a 15-minute bucket is complete only when its third 5-minute bar is in
    counts: dict[dt.datetime, int] = {}
    for b in done5:
        m = b.ts.hour * 60 + b.ts.minute - (9 * 60 + 15)
        key = b.ts.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=9 * 60 + 15 + (m // 15) * 15)
        counts[key] = counts.get(key, 0) + 1
    b15 = [b for b in b15 if counts.get(b.ts, 0) >= 3]
    if len(b15) < 30:
        return None
    h = [b.high for b in b15]; l = [b.low for b in b15]; c = [b.close for b in b15]
    st2, _ = supertrend(h, l, c, 10, 2.0)
    st3, _ = supertrend(h, l, c, 10, 3.0)
    ax, _, _ = adx(h, l, c, 14)
    return TrendState(b15[-1].ts + dt.timedelta(minutes=15), st2[-1], st2[-2], st3[-1], ax[-1])


def trading_days_left(today: dt.date, expiry: dt.date) -> int:
    n, d = 0, today
    while d < expiry:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def single_strike(spot: float, direction: int, days_left: int, now_t: dt.time, step: float) -> float:
    """Time-decay-aware strike: ATM with >= 3 trading days left, 2 steps ITM with 2 days, 4 steps ITM with <= 1 day
    (or after 13:30), so theta eats less of the premium as expiry nears."""
    atm = round(spot / step) * step
    if days_left <= 1 or now_t >= dt.time(13, 30):
        return atm - direction * 4 * step
    if days_left == 2:
        return atm - direction * 2 * step
    return atm


def _q_ok(q) -> bool:
    return q is not None and q.ltp > 0


def build_single(chain: Chain, direction: int, now: dt.datetime, why: str) -> Optional[TradePlan]:
    step = chain.step or 100.0
    kind = "CE" if direction > 0 else "PE"
    dl = trading_days_left(now.date(), chain.expiry)
    k = single_strike(chain.spot, direction, dl, now.time(), step)
    q = chain.get(k, kind)
    if not _q_ok(q):
        return None
    return TradePlan(name=f"Long {kind}", regime="TREND", expiry=chain.expiry, legs=(Leg(q.contract, "BUY", q.ltp),),
                     lot_size=q.contract.lot_size, max_loss_unit=q.ltp, max_profit_unit=10 * q.ltp,
                     rationale=f"[PLAYBOOK] {why}; {k:.0f}{kind} @ {q.ltp:.1f}, {dl}d to expiry",
                     variant_id=f"{PREFIX}single_{'UP' if direction > 0 else 'DOWN'}", sl_frac=None, tp_frac=None)


def build_condor(chain: Chain, p: PlaybookParams, why: str) -> Optional[TradePlan]:
    step = chain.step or 100.0
    S = chain.spot
    atm = round(S / step) * step
    off = max(round(p.condor_short_pct * S / step), 1) * step
    wing = max(round(p.condor_wing_pct * S / step), 1) * step
    sc, sp = chain.get(atm + off, "CE"), chain.get(atm - off, "PE")
    lc, lp = chain.get(atm + off + wing, "CE"), chain.get(atm - off - wing, "PE")
    if not all(_q_ok(x) for x in (sc, sp, lc, lp)):
        return None
    credit = sc.ltp + sp.ltp - lc.ltp - lp.ltp
    if credit <= 0:
        return None
    return TradePlan(name="Iron condor", regime="RANGE", expiry=chain.expiry,
                     legs=(Leg(lc.contract, "BUY", lc.ltp), Leg(lp.contract, "BUY", lp.ltp),
                           Leg(sc.contract, "SELL", sc.ltp), Leg(sp.contract, "SELL", sp.ltp)),
                     lot_size=sc.contract.lot_size, max_loss_unit=wing - credit, max_profit_unit=credit,
                     rationale=f"[PLAYBOOK] {why}; sell {atm - off:.0f}P/{atm + off:.0f}C, wings "
                               f"{atm - off - wing:.0f}P/{atm + off + wing:.0f}C, credit {credit:.1f}",
                     variant_id=f"{PREFIX}condor", sl_frac=None, tp_frac=None)


def is_playbook(plan: TradePlan) -> bool:
    return plan.variant_id.startswith(PREFIX)


def plan_direction(plan: TradePlan) -> int:
    return 1 if plan.variant_id.endswith("_UP") else (-1 if plan.variant_id.endswith("_DOWN") else 0)


@dataclass
class DayState:
    day: Optional[dt.date] = None
    singles: int = 0
    multi_done: bool = False
    last_signal_bar: Optional[str] = None     # 15m bar end already acted on (one entry per signal bar)

    def roll(self, today: dt.date) -> None:
        if self.day != today:
            self.day, self.singles, self.multi_done, self.last_signal_bar = today, 0, False, None

    def export(self) -> dict:
        return {"day": self.day.isoformat() if self.day else None, "singles": self.singles,
                "multi_done": self.multi_done, "last_signal_bar": self.last_signal_bar}

    @classmethod
    def restore(cls, d: dict) -> "DayState":
        s = cls()
        try:
            s.day = dt.date.fromisoformat(d["day"]) if d.get("day") else None
            s.singles, s.multi_done = int(d.get("singles", 0)), bool(d.get("multi_done", False))
            s.last_signal_bar = d.get("last_signal_bar")
        except (KeyError, ValueError, TypeError):
            pass
        return s


@dataclass(frozen=True)
class Decision:
    action: str               # "single" | "multi" | "none"
    direction: int = 0
    why: str = ""
    bar_key: Optional[str] = None


class Playbook:
    def __init__(self, p: Optional[PlaybookParams] = None) -> None:
        self.p = p or PlaybookParams()
        self.day = DayState()

    # ---------------------------------------------------------------- entries
    def single_signal(self, ts: Optional[TrendState], now: dt.datetime, vix: float) -> Decision:
        """A fresh confirmed trend-start bar inside the window, not yet acted on."""
        self.day.roll(now.date())
        p = self.p
        if ts is None or not ts.signal:
            return Decision("none")
        key = ts.bar_end.isoformat()
        if key == self.day.last_signal_bar:
            return Decision("none")
        if ts.bar_end.date() != now.date() or now - ts.bar_end > dt.timedelta(minutes=5):
            return Decision("none")                      # stale bar (e.g. after a restart): never chase it
        if not (p.single_t0 <= ts.bar_end.time() <= p.single_t1):
            return Decision("none")
        if not (vix >= p.single_min_vix) or self.day.singles >= p.single_max_per_day:
            return Decision("none")
        why = (f"15m Supertrend(10,2) flipped {'UP' if ts.signal > 0 else 'DOWN'}, ST(10,3) agrees, "
               f"ADX {ts.adx:.0f}, VIX {vix:.1f}")
        return Decision("single", ts.signal, why, key)

    def multi_signal(self, ts: Optional[TrendState], now: dt.datetime, vix: float) -> Decision:
        self.day.roll(now.date())
        p = self.p
        if ts is None or self.day.multi_done or self.day.singles > 0:
            return Decision("none")
        if not (p.multi_t0 <= now.time() <= p.multi_t1):
            return Decision("none")
        if not (vix >= p.multi_min_vix) or not (ts.adx < p.multi_max_adx) or math.isnan(ts.adx):
            return Decision("none")
        return Decision("multi", 0, f"no trend (15m ADX {ts.adx:.0f} < {p.multi_max_adx:.0f}), VIX {vix:.1f} high")

    def took_single(self, d: Decision) -> None:
        self.day.singles += 1
        self.day.last_signal_bar = d.bar_key

    def skipped_signal(self, d: Decision) -> None:
        """A signal that could not be traded (quotes, margin): do not retry the same bar every second."""
        self.day.last_signal_bar = d.bar_key

    def took_multi(self) -> None:
        self.day.multi_done = True

    # ------------------------------------------------------------------ exits
    def exit_reason(self, plan: TradePlan, pnl: float, ts: Optional[TrendState], pending_single: bool) -> Optional[str]:
        """No stop-loss, no trailing stop. Square-off at 15:15 is handled by the caller."""
        p = self.p
        if p.loss_cap_rs > 0 and pnl <= -p.loss_cap_rs:
            return f"LOSS_CAP_{p.loss_cap_rs:.0f}"
        d = plan_direction(plan)
        if d and ts is not None and ts.st2 == -d:
            # the reversal bar is not re-used as a new entry (the backtest skips the bar it exits on)
            self.day.roll(ts.bar_end.date())
            self.day.last_signal_bar = ts.bar_end.isoformat()
            return "TREND_REVERSAL"
        if d == 0 and pending_single:
            return "TREND_SIGNAL_PRIORITY"           # close the condor; the single-leg trade is entered next
        return None
