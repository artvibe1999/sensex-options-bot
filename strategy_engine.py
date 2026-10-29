"""Greeks/IV, option-chain snapshot, regime detection and strategy selection."""
from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from config import IST, StrategyParams
from zerodha_client import OptionContract, ZerodhaClient

log = logging.getLogger(__name__)


# ------------------------------------------------------------ Black-Scholes
def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day, in index points
    vega: float   # per 1 vol point


def bs_price(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if kind == "CE" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> Greeks:
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    decay = -S * _pdf(d1) * sigma / (2 * sq)
    if kind == "CE":
        delta = _cdf(d1)
        theta = decay - r * K * math.exp(-r * T) * _cdf(d2)
    else:
        delta = _cdf(d1) - 1.0
        theta = decay + r * K * math.exp(-r * T) * _cdf(-d2)
    return Greeks(delta, _pdf(d1) / (S * sigma * sq), theta / 365.0, S * _pdf(d1) * sq / 100.0)


def implied_vol(price: float, S: float, K: float, T: float, r: float, kind: str) -> Optional[float]:
    """Bisection IV solver; None if the price is outside no-arbitrage bounds."""
    if T <= 0 or price <= 0:
        return None
    lo, hi = 0.01, 3.0
    if not (bs_price(S, K, T, r, lo, kind) <= price <= bs_price(S, K, T, r, hi, kind)):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, r, mid, kind) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-5:
            break
    return 0.5 * (lo + hi)


# ------------------------------------------------------------ chain model
@dataclass(frozen=True)
class OptionQuote:
    contract: OptionContract
    ltp: float
    oi: float
    iv: Optional[float]
    greeks: Optional[Greeks]
    volume: float = 0.0
    bid: Optional[float] = None       # best bid / ask from market depth (None if not quoted)
    ask: Optional[float] = None


@dataclass
class Chain:
    expiry: dt.date
    spot: float
    atm: float
    step: float
    pcr: float
    quotes: dict[tuple[float, str], OptionQuote] = field(default_factory=dict)

    def get(self, strike: float, kind: str) -> Optional[OptionQuote]:
        q = self.quotes.get((strike, kind))
        return q if q and q.ltp > 0 else None


@dataclass(frozen=True)
class Leg:
    contract: OptionContract
    side: str  # BUY / SELL
    ref_price: float
    ratio: int = 1     # multiples of the lot quantity (ratio spreads sell 2x the lots they buy)


@dataclass(frozen=True)
class TradePlan:
    name: str
    regime: str
    expiry: dt.date
    legs: tuple[Leg, ...]
    lot_size: int
    max_loss_unit: float    # points per unit, worst case
    max_profit_unit: float
    rationale: str
    variant_id: str = ""
    sl_frac: Optional[float] = None   # hard stop as a fraction of max loss (None: RiskParams default)
    tp_frac: Optional[float] = None   # take-profit as a fraction of max profit
    # Underlying-price exits for signal trades (intraday.py): exit when SENSEX trades at/below spot_lo or at/above
    # spot_hi. Once it reaches spot_be, the adverse bound moves to entry_spot (breakeven). None = not used.
    spot_lo: Optional[float] = None
    spot_hi: Optional[float] = None
    spot_be: Optional[float] = None
    entry_spot: Optional[float] = None

    @property
    def spot_rules(self) -> bool:
        return self.spot_lo is not None or self.spot_hi is not None

    def qty_for(self, leg: "Leg", lots: int = 1) -> int:
        return lots * self.lot_size * leg.ratio

    @property
    def max_loss_per_lot(self) -> float:
        return self.max_loss_unit * self.lot_size

    @property
    def max_profit_per_lot(self) -> float:
        return self.max_profit_unit * self.lot_size


# ---------------------------------------------------------------- regime
class Regime(str, Enum):
    LOW_VOL = "LOW_VOL"
    HIGH_VOL = "HIGH_VOL"
    NORMAL = "NORMAL"


@dataclass(frozen=True)
class RegimeState:
    regime: Regime
    vix: float
    atr_pct: float
    realized_vol: float
    spike: bool
    score: int


class RegimeDetector:
    """Votes from VIX, daily ATR% and realised vol, with hysteresis."""

    def __init__(self, p: StrategyParams) -> None:
        self.p = p
        self.current = Regime.NORMAL
        self._vix_hist: deque[tuple[float, float]] = deque()

    @staticmethod
    def atr_pct(candles: Sequence[dict], n: int = 14) -> float:
        if len(candles) < n + 1:
            return float("nan")
        trs = []
        for prev, cur in zip(candles[-n - 1:-1], candles[-n:]):
            trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]),
                           abs(cur["low"] - prev["close"])))
        return 100.0 * statistics.mean(trs) / candles[-1]["close"]

    @staticmethod
    def realized_vol(candles: Sequence[dict], n: int = 20) -> float:
        closes = [c["close"] for c in candles[-n - 1:]]
        if len(closes) < n + 1:
            return float("nan")
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        return 100.0 * statistics.stdev(rets) * math.sqrt(252)

    def update(self, vix: float, daily: Sequence[dict], t: Optional[float] = None) -> RegimeState:
        """``t`` is a monotonic-style clock in seconds; the backtester passes simulated time."""
        p, now = self.p, (time.monotonic() if t is None else t)
        self._vix_hist.append((now, vix))
        while self._vix_hist and now - self._vix_hist[0][0] > 900:
            self._vix_hist.popleft()
        ref = self._vix_hist[0][1]
        spike = ref > 0 and (vix / ref - 1.0) >= p.vix_spike_pct

        atr, rv = self.atr_pct(daily), self.realized_vol(daily)
        score = 0
        score += 1 if vix >= p.vix_high else -1 if vix <= p.vix_low else 0
        if not math.isnan(atr):
            score += 1 if atr >= p.atr_pct_high else -1 if atr <= p.atr_pct_low else 0
        if not math.isnan(rv):
            score += 1 if rv >= p.rv_high else -1 if rv <= p.rv_low else 0

        if score >= 2 or (self.current is Regime.HIGH_VOL and score >= 1):
            new = Regime.HIGH_VOL
        elif score <= -2 or (self.current is Regime.LOW_VOL and score <= -1):
            new = Regime.LOW_VOL
        else:
            new = Regime.NORMAL
        if new is not self.current:
            log.info("Regime %s -> %s (vix=%.2f atr%%=%.2f rv=%.2f score=%d)",
                     self.current.value, new.value, vix, atr, rv, score)
        self.current = new
        return RegimeState(new, vix, atr, rv, spike, score)


def ema(values: Sequence[float], n: int) -> float:
    k = 2.0 / (n + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def trend_direction(closes: Sequence[float], p: StrategyParams) -> Optional[int]:
    """+1 up-trend, -1 down-trend, 0 flat, None if there is not enough history."""
    if len(closes) < p.ema_slow + 1:
        return None
    fast, slow = ema(closes, p.ema_fast), ema(closes, p.ema_slow)
    if fast > slow * (1 + p.trend_buffer_pct):
        return 1
    if fast < slow * (1 - p.trend_buffer_pct):
        return -1
    return 0


# ---------------------------------------------------------------- engine
class StrategyEngine:
    def __init__(self, client: ZerodhaClient, params: StrategyParams) -> None:
        self.client, self.p = client, params
        self.use_stream = True          # build the chain from WebSocket ticks (1 s cadence); REST is the fallback
        self.last_source = ""
        self._last_rest = -1e9
        self._step: Optional[float] = None

    def pick_expiry(self, today: dt.date) -> Optional[dt.date]:
        ok = [e for e in self.client.expiries() if self.p.min_dte <= (e - today).days <= self.p.max_dte]
        return min(ok) if ok else None

    def _stream_raw(self, sel: list) -> Optional[dict]:
        """Quote-shaped dict built from WebSocket FULL ticks (no REST call, so no rate limit).
        None unless nearly the whole window has ticked and the strikes around ATM are current."""
        self.client.subscribe_full({c.token: c.key for c in sel})        # idempotent
        flow = self.client.flow_snapshot(max_age_s=3600.0)
        fresh = self.client.flow_snapshot(max_age_s=self.p.stream_near_age_s)
        raw: dict = {}
        for c in sel:
            f = flow.get(c.token)
            if f and f.get("ltp"):
                raw[c.key] = {"last_price": f["ltp"], "oi": f.get("oi") or 0, "volume": f.get("volume") or 0,
                              "depth": {"buy": [{"price": f["bid"]}] if f.get("bid") else [],
                                        "sell": [{"price": f["ask"]}] if f.get("ask") else []}}
        if len(raw) < self.p.stream_min_coverage * len(sel):
            return None
        mid = sorted(c.strike for c in sel)[len(sel) // 2]
        near = [c for c in sel if abs(c.strike - mid) <= 3 * (self._step or 100.0)]
        if near and sum(1 for c in near if c.token in fresh) < 0.5 * len(near):
            return None                                                    # feed stalled around ATM
        return raw

    def build_chain(self, expiry: dt.date, spot: float, now: dt.datetime) -> Optional[Chain]:
        contracts = self.client.contracts_for(expiry)
        strikes = sorted({c.strike for c in contracts})
        if len(strikes) < 3:
            return None
        step = min(b - a for a, b in zip(strikes, strikes[1:]))  # derived, not assumed
        self._step = step
        atm = min(strikes, key=lambda k: abs(k - spot))
        idx = strikes.index(atm)
        w = self.p.chain_window_strikes
        window = set(strikes[max(0, idx - w): idx + w + 1])
        sel = [c for c in contracts if c.strike in window]
        raw = self._stream_raw(sel) if self.use_stream else None
        self.last_source = "stream"
        if raw is None:                                   # warm-up, stalled feed or stream disabled
            if time.monotonic() - self._last_rest < self.p.rest_min_gap_s:
                return None                               # Kite allows ~1 quote request/s: never exceed it
            self._last_rest = time.monotonic()
            raw = self.client.quote([c.key for c in sel])
            self.last_source = "rest"

        expiry_dt = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=IST)
        T = max((expiry_dt - now).total_seconds(), 60.0) / (365 * 24 * 3600)
        chain = Chain(expiry, spot, atm, step, 0.0)
        put_oi = call_oi = 0.0
        for c in sel:
            q = raw.get(c.key)
            if not q:
                continue
            ltp, oi = float(q["last_price"]), float(q.get("oi") or 0)
            iv = implied_vol(ltp, spot, c.strike, T, self.p.risk_free_rate, c.kind)
            g = bs_greeks(spot, c.strike, T, self.p.risk_free_rate, iv, c.kind) if iv else None
            depth = q.get("depth") or {}
            buy, sell = depth.get("buy") or [], depth.get("sell") or []
            bid = float(buy[0]["price"]) if buy and buy[0].get("price") else None
            ask = float(sell[0]["price"]) if sell and sell[0].get("price") else None
            chain.quotes[(c.strike, c.kind)] = OptionQuote(c, ltp, oi, iv, g, float(q.get("volume") or 0), bid, ask)
            if c.kind == "PE":
                put_oi += oi
            else:
                call_oi += oi
        chain.pcr = put_oi / call_oi if call_oi else float("nan")
        return chain
