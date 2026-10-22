"""Option-chain features beyond price and delta: open-interest structure, gamma exposure, skew, OI flow.

Computed from every fresh chain snapshot (default every 10 s), stored in the `features` table and made
available to strategy variants whose `oi_rule` is not "none".

IMPORTANT: none of these is a proven edge. They are hypotheses. Variants that use them are shadow-tested
on live data and only promoted by promote.py if they beat their plain (unfiltered) twin out of sample.
Definitions (all approximations of widely used market-structure heuristics):

  pcr        put OI / call OI over the quoted window
  call_wall  strike ABOVE spot with the largest call OI (often acts as resistance)
  put_wall   strike BELOW spot with the largest put OI (often acts as support)
  max_pain   strike minimising the total intrinsic value payable to option buyers
  net_gex    sum(gamma x OI x spot^2 x 0.01), calls positive, puts negative. A conventional proxy for
             dealer gamma exposure per 1% move; the sign convention assumes dealers are long calls and
             short puts, which is DISPUTED. Treat the sign as a regime hint, not a fact.
  skew       IV(put nearest 0.25 delta) - IV(call nearest 0.25 delta), in vol points
  *_oi_chg   change in total call/put OI versus a reference snapshot about 5 minutes ago
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

from strategy_engine import Chain


@dataclass(frozen=True)
class ChainFeatures:
    pcr: float
    call_wall: Optional[float]
    put_wall: Optional[float]
    max_pain: Optional[float]
    net_gex: float
    atm_iv: Optional[float]
    skew: Optional[float]
    call_oi_chg: Optional[float]
    put_oi_chg: Optional[float]
    pcr_chg: Optional[float]
    atm_spread_pct: Optional[float]
    atm_volume: float


def _pcr(chain: Chain) -> float:
    call = sum(q.oi for q in chain.quotes.values() if q.contract.kind == "CE")
    put = sum(q.oi for q in chain.quotes.values() if q.contract.kind == "PE")
    return put / call if call else float("nan")


def _wall(chain: Chain, kind: str) -> Optional[float]:
    cands = []
    for (strike, k), q in chain.quotes.items():
        if k != kind or q.oi <= 0:
            continue
        if (kind == "CE" and strike <= chain.spot) or (kind == "PE" and strike >= chain.spot):
            continue
        cands.append((q.oi, -abs(strike - chain.spot), strike))   # ties prefer the strike nearest spot
    return max(cands)[2] if cands else None


def _max_pain(chain: Chain) -> Optional[float]:
    strikes = sorted({s for s, _ in chain.quotes})
    if not strikes:
        return None
    best: Optional[tuple[float, float]] = None
    for k in strikes:
        pain = 0.0
        for (s, kind), q in chain.quotes.items():
            if kind == "CE" and k > s:
                pain += (k - s) * q.oi
            elif kind == "PE" and k < s:
                pain += (s - k) * q.oi
        if best is None or pain < best[0]:
            best = (pain, k)
    return best[1] if best else None


def _nearest_delta_iv(chain: Chain, kind: str, target: float) -> Optional[float]:
    best: Optional[tuple[float, float]] = None
    for q in chain.quotes.values():
        if q.contract.kind != kind or q.greeks is None or q.iv is None:
            continue
        d = abs(abs(q.greeks.delta) - target)
        if best is None or d < best[0]:
            best = (d, q.iv)
    return best[1] if best and best[0] < 0.1 else None


def compute(chain: Chain, ref: Optional[Chain] = None) -> ChainFeatures:
    gex = 0.0
    for q in chain.quotes.values():
        if q.greeks is not None and q.oi > 0:
            sign = 1.0 if q.contract.kind == "CE" else -1.0
            gex += sign * q.greeks.gamma * q.oi * chain.spot ** 2 * 0.01
    atm = [chain.quotes.get((chain.atm, "CE")), chain.quotes.get((chain.atm, "PE"))]
    atm = [q for q in atm if q is not None]
    ivs = [q.iv for q in atm if q.iv]
    spreads = []
    for q in atm:
        if q.bid and q.ask and q.ask >= q.bid > 0:
            spreads.append((q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100.0)
    ivp, ivc = _nearest_delta_iv(chain, "PE", 0.25), _nearest_delta_iv(chain, "CE", 0.25)

    call_chg = put_chg = pcr_chg = None
    if ref is not None and ref.expiry == chain.expiry:
        call_chg = put_chg = 0.0
        for key, q in chain.quotes.items():
            r = ref.quotes.get(key)
            if r is None:
                continue
            if key[1] == "CE":
                call_chg += q.oi - r.oi
            else:
                put_chg += q.oi - r.oi
        a, b = _pcr(chain), _pcr(ref)
        pcr_chg = None if math.isnan(a) or math.isnan(b) else a - b
    return ChainFeatures(
        pcr=_pcr(chain), call_wall=_wall(chain, "CE"), put_wall=_wall(chain, "PE"),
        max_pain=_max_pain(chain), net_gex=gex,
        atm_iv=sum(ivs) / len(ivs) if ivs else None,
        skew=(ivp - ivc) * 100.0 if ivp is not None and ivc is not None else None,
        call_oi_chg=call_chg, put_oi_chg=put_chg, pcr_chg=pcr_chg,
        atm_spread_pct=sum(spreads) / len(spreads) if spreads else None,
        atm_volume=sum(q.volume for q in atm))


class FeatureTracker:
    """Keeps a short history of chains so OI CHANGE can be measured against ~5 minutes ago."""

    def __init__(self, ref_age_s: float = 300.0, sample_every_s: float = 60.0, keep: int = 30) -> None:
        self.ref_age_s, self.sample_every_s = ref_age_s, sample_every_s
        self._hist: deque[tuple[float, Chain]] = deque(maxlen=keep)

    def reset(self) -> None:
        self._hist.clear()

    def update(self, t: float, chain: Chain) -> ChainFeatures:
        ref: Optional[Chain] = None
        for ts, c in self._hist:                       # oldest -> newest: take the newest old-enough sample
            if t - ts >= self.ref_age_s and c.expiry == chain.expiry:
                ref = c
        if not self._hist or t - self._hist[-1][0] >= self.sample_every_s:
            self._hist.append((t, chain))
        return compute(chain, ref)
