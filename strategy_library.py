"""Library of defined-risk SENSEX option strategy variants.

IMPORTANT: these are CANDIDATES, not "proven" strategies. Each variant is a
template (iron condor, credit/debit spread, straddle...) with a parameter set,
a volatility-regime filter, a days-to-expiry band, a trend filter and exit
rules. A variant only trades real or paper money after it passes the gates:

  1. backtest.py   walk-forward backtest -> data/approved_backtest.json
  2. paper mode    ShadowBook forward-tests ALL variants live, every day
  3. promote.py    forward results + backtest -> data/approved_live.json

All variants are defined-risk (no naked short options), so worst-case loss per
lot is known at entry and the position sizer can enforce a hard risk budget.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from chain_features import ChainFeatures
from intraday import BREAKOUT, RANGE, TRAP, Signal, SignalParams, detect
from strategy_engine import Chain, Leg, OptionQuote, RegimeState, TradePlan
from option_strategies import STRATEGY_BUILDERS, STRATEGY_DIRECTION
from trend_regime import TrendInfo

log = logging.getLogger(__name__)

LOW, NORM, HIGH = "LOW_VOL", "NORMAL", "HIGH_VOL"
DTE_BANDS = ((1, 3), (4, 7))
MIN_CREDIT_FRAC = 0.10      # minimum net credit as a fraction of spread width
MIN_DEBIT_RR = 0.8          # minimum reward/risk for debit spreads


@dataclass(frozen=True)
class StrategySpec:
    id: str
    template: str
    params: tuple                      # ((key, value), ...) hashable
    regimes: tuple
    dte_min: int
    dte_max: int
    trend: str = "none"                # none | with | flat | dvalid | dtrap (daily, backtestable) | ivalid | itrap (intraday)
    entry_after: dt.time = dt.time(9, 30)
    sl_frac: float = 0.6               # hard stop as a fraction of max loss
    tp_frac: float = 0.6               # take-profit as a fraction of max profit
    max_cost_frac: float = 0.0         # >0: skip when round-trip costs exceed this share of the take-profit amount
    oi_rule: str = "none"              # none | walls | pcr_calm | gex_pos | gex_neg | flow  (needs live OI data)

    @property
    def p(self) -> dict:
        return dict(self.params)

    @property
    def needs_oi(self) -> bool:
        """OI-rule variants cannot be backtested (no historical OI): they are validated forward only."""
        return self.oi_rule != "none"

    @property
    def intraday_trend(self) -> bool:
        """Intraday valid-trend / trap variants need 5-minute data: validated forward only."""
        return self.trend in ("ivalid", "itrap")

    @property
    def forward_only(self) -> bool:
        return self.needs_oi or self.intraday_trend

    @property
    def intraday_signal(self) -> bool:
        """Price-action signal variants (intraday.py): backtested on 5-minute data by intraday_backtest.py."""
        return self.template.startswith("ib_")

    @property
    def multi_leg(self) -> bool:
        """More than one option leg. Small accounts trade single-leg structures only (Config.multi_leg_min_capital)."""
        return self.template not in SINGLE_LEG_TEMPLATES

    @property
    def base_id(self) -> str:
        """Id of the plain twin (same trade without the OI / intraday-trend filter) used as the benchmark."""
        b = self.id.split("|oi=")[0]
        return b.replace(f"|t{self.trend}|", "|tnone|") if self.intraday_trend else b


SINGLE_LEG_TEMPLATES = frozenset({"long_call", "long_put", "ib_long"})
SIG_KIND = {1: BREAKOUT, 2: TRAP, 3: RANGE}


# ------------------------------------------------------------------ intraday signal context
class IntradayContext:
    """Today's completed 5-minute bars and levels; signals are computed once per (signal parameters, last bar)."""

    def __init__(self, bars, levels, kl=None) -> None:
        self.bars, self.levels, self.kl = bars, levels, kl
        self._cache: dict = {}

    def signal(self, spec: "StrategySpec") -> Optional[Signal]:
        if not self.bars or self.levels is None:
            return None
        kind, sp = SIG_KIND[int(spec.p["sig"])], signal_params(spec)
        key = (kind, sp, len(self.bars), self.bars[-1].ts)
        if key not in self._cache:
            self._cache[key] = detect(self.bars, self.levels, sp, kind, self.kl)
        sig = self._cache[key]
        want = int(spec.p.get("dir", 0))              # 0 both directions, -1 bearish only, +1 bullish only
        return sig if sig is not None and (want == 0 or sig.direction == want) else None


def signal_params(spec: "StrategySpec") -> SignalParams:
    """Signal settings of an ib_* variant. `flt` 1 switches on the sideways-day filters that held up out of sample
    (wide CPR, no range expansion, choppy session); 0 is the unfiltered baseline."""
    p = spec.p
    flt = int(p.get("flt", 0))
    return SignalParams(confirm=int(p.get("conf", 1)), er_min=float(p.get("er", 0.3)),
                        cpr_wide_pct=0.25 if flt else float(p.get("cpr", 0.0)),
                        min_range_adr=0.5 if flt else 0.0, day_er_min=0.2 if flt else 0.0,
                        target_r=float(p.get("tr", 2.0)))


# ------------------------------------------------------------------ helpers
def _otm_by_delta(chain: Chain, kind: str, target: float) -> Optional[OptionQuote]:
    best: Optional[tuple[float, OptionQuote]] = None
    for q in chain.quotes.values():
        c = q.contract
        if c.kind != kind or q.greeks is None or q.ltp <= 0:
            continue
        if (kind == "CE" and c.strike <= chain.spot) or (kind == "PE" and c.strike >= chain.spot):
            continue
        d = abs(abs(q.greeks.delta) - target)
        if best is None or d < best[0]:
            best = (d, q)
    return best[1] if best else None


Built = Optional[tuple[tuple[Leg, ...], float, float, str]]  # legs, max_loss, max_profit, why


def _leg(q: OptionQuote, side: str) -> Leg:
    return Leg(q.contract, side, q.ltp)


def _iron_condor(chain: Chain, p: dict) -> Built:
    sc, sp = _otm_by_delta(chain, "CE", p["delta"]), _otm_by_delta(chain, "PE", p["delta"])
    if not sc or not sp:
        return None
    w = p["wing"]
    lc, lp = chain.get(sc.contract.strike + w, "CE"), chain.get(sp.contract.strike - w, "PE")
    if not lc or not lp:
        return None
    credit = sc.ltp + sp.ltp - lc.ltp - lp.ltp
    if credit < MIN_CREDIT_FRAC * w:
        return None
    legs = (_leg(sc, "SELL"), _leg(sp, "SELL"), _leg(lc, "BUY"), _leg(lp, "BUY"))
    return legs, w - credit, credit, f"shorts {sp.contract.strike:.0f}/{sc.contract.strike:.0f} credit {credit:.1f}"


def _iron_fly(chain: Chain, p: dict) -> Built:
    w = p["wing"]
    sc, sp = chain.get(chain.atm, "CE"), chain.get(chain.atm, "PE")
    lc, lp = chain.get(chain.atm + w, "CE"), chain.get(chain.atm - w, "PE")
    if not (sc and sp and lc and lp):
        return None
    credit = sc.ltp + sp.ltp - lc.ltp - lp.ltp
    if credit < 0.15 * w:
        return None
    legs = (_leg(sc, "SELL"), _leg(sp, "SELL"), _leg(lc, "BUY"), _leg(lp, "BUY"))
    return legs, w - credit, credit, f"fly @{chain.atm:.0f} credit {credit:.1f}"


def _strike_beyond(chain: Chain, level: float, side: int) -> float:
    """First strike on the chain's grid at or beyond `level` (side +1 above, -1 below)."""
    k = (level // chain.step) * chain.step
    if side > 0 and k < level:
        k += chain.step
    return k


def _build_intraday(spec: "StrategySpec", chain: Chain, sig: Signal) -> Built:
    """Legs for an intraday signal. ib_long: one option in the signal direction (otm = strikes out of the money);
    ib_debit: bull call / bear put spread from ATM; ib_credit: sell the spread BEYOND the signal stop on the side the
    trapped traders are on (where option writers sell); ib_condor: shorts beyond the range day's high and low."""
    p, d, step = spec.p, sig.direction, chain.step
    if spec.template == "ib_long":
        kind = "CE" if d > 0 else "PE"
        q = chain.get(chain.atm + d * int(p.get("otm", 0)) * step, kind)
        if not q:
            return None
        return (_leg(q, "BUY"),), q.ltp, q.ltp, f"{sig.reason}: long {q.contract.strike:.0f}{kind[0]} @ {q.ltp:.1f}"
    if spec.template == "ib_debit":
        return _debit_spread(chain, {"width": p["width"]}, "CE" if d > 0 else "PE", d)
    if spec.template == "ib_credit":
        w, off = p["width"], p.get("off", 0)
        if d < 0:                                     # bearish: bear call spread above the trap / breakdown stop
            ks = _strike_beyond(chain, sig.stop, 1) + off
            sq, lq = chain.get(ks, "CE"), chain.get(ks + w, "CE")
        else:                                         # bullish: bull put spread below the stop
            ks = _strike_beyond(chain, sig.stop, -1) - off
            sq, lq = chain.get(ks, "PE"), chain.get(ks - w, "PE")
        if not sq or not lq:
            return None
        credit = sq.ltp - lq.ltp
        if credit < MIN_CREDIT_FRAC * w:
            return None
        return ((_leg(sq, "SELL"), _leg(lq, "BUY")), w - credit, credit,
                f"{sig.reason}: short {ks:.0f}{sq.contract.kind[0]} credit {credit:.1f}")
    if spec.template == "ib_condor":
        w, off = p["width"], p.get("off", 0)
        kc, kp = _strike_beyond(chain, sig.hi, 1) + off, _strike_beyond(chain, sig.lo, -1) - off
        sc, sp_, lc, lp = chain.get(kc, "CE"), chain.get(kp, "PE"), chain.get(kc + w, "CE"), chain.get(kp - w, "PE")
        if not (sc and sp_ and lc and lp):
            return None
        credit = sc.ltp + sp_.ltp - lc.ltp - lp.ltp
        if credit < MIN_CREDIT_FRAC * w:
            return None
        return ((_leg(sc, "SELL"), _leg(sp_, "SELL"), _leg(lc, "BUY"), _leg(lp, "BUY")), w - credit, credit,
                f"{sig.reason}: condor {kp:.0f}/{kc:.0f} credit {credit:.1f}")
    raise ValueError(spec.template)


def _spot_bounds(spec: "StrategySpec", sig: Signal) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(spot_lo, spot_hi, spot_be): exit at/below lo or at/above hi; at spot_be the adverse bound moves to entry."""
    if sig.kind == RANGE:
        buf = sig.entry * 0.001
        return sig.lo - buf, sig.hi + buf, None
    d = sig.direction
    be = sig.entry + d * sig.risk if int(spec.p.get("be", 0)) else None
    if spec.template == "ib_credit":                  # premium sellers keep the trade unless the idea is invalidated
        return (sig.stop, None, None) if d > 0 else (None, sig.stop, None)
    return (sig.stop, sig.target, be) if d > 0 else (sig.target, sig.stop, be)


def _bull_put(chain: Chain, p: dict) -> Built:
    sp = _otm_by_delta(chain, "PE", p["delta"])
    if not sp:
        return None
    lp = chain.get(sp.contract.strike - p["width"], "PE")
    if not lp:
        return None
    credit = sp.ltp - lp.ltp
    if credit < 0.15 * p["width"]:
        return None
    return (_leg(sp, "SELL"), _leg(lp, "BUY")), p["width"] - credit, credit, \
        f"short {sp.contract.strike:.0f}P credit {credit:.1f}"


def _bear_call(chain: Chain, p: dict) -> Built:
    sc = _otm_by_delta(chain, "CE", p["delta"])
    if not sc:
        return None
    lc = chain.get(sc.contract.strike + p["width"], "CE")
    if not lc:
        return None
    credit = sc.ltp - lc.ltp
    if credit < 0.15 * p["width"]:
        return None
    return (_leg(sc, "SELL"), _leg(lc, "BUY")), p["width"] - credit, credit, \
        f"short {sc.contract.strike:.0f}C credit {credit:.1f}"


def _debit_spread(chain: Chain, p: dict, kind: str, direction: int) -> Built:
    long_q = chain.get(chain.atm, kind)
    short_q = chain.get(chain.atm + direction * p["width"], kind)
    if not long_q or not short_q:
        return None
    debit = long_q.ltp - short_q.ltp
    reward = p["width"] - debit
    if debit <= 0 or reward <= 0 or reward / debit < MIN_DEBIT_RR:
        return None
    return (_leg(long_q, "BUY"), _leg(short_q, "SELL")), debit, reward, f"debit {debit:.1f}"


def _bull_call_debit(chain: Chain, p: dict) -> Built:
    return _debit_spread(chain, p, "CE", 1)


def _bear_put_debit(chain: Chain, p: dict) -> Built:
    return _debit_spread(chain, p, "PE", -1)


def _single_leg(chain: Chain, p: dict, kind: str) -> Built:
    q = _otm_by_delta(chain, kind, p["delta"])
    if not q:
        return None
    return (_leg(q, "BUY"),), q.ltp, q.ltp * p["tp_mult"], f"long {q.contract.strike:.0f}{kind[0]} premium {q.ltp:.1f}"


def _long_call(chain: Chain, p: dict) -> Built:
    return _single_leg(chain, p, "CE")


def _long_put(chain: Chain, p: dict) -> Built:
    return _single_leg(chain, p, "PE")


def _long_straddle(chain: Chain, p: dict) -> Built:
    ce, pe = chain.get(chain.atm, "CE"), chain.get(chain.atm, "PE")
    if not ce or not pe:
        return None
    debit = ce.ltp + pe.ltp
    return (_leg(ce, "BUY"), _leg(pe, "BUY")), debit, debit * p["tp_mult"], f"straddle debit {debit:.1f}"


def _long_strangle(chain: Chain, p: dict) -> Built:
    ce, pe = _otm_by_delta(chain, "CE", p["delta"]), _otm_by_delta(chain, "PE", p["delta"])
    if not ce or not pe:
        return None
    debit = ce.ltp + pe.ltp
    return (_leg(ce, "BUY"), _leg(pe, "BUY")), debit, debit * p["tp_mult"], f"strangle debit {debit:.1f}"


_BUILDERS: dict[str, Callable[[Chain, dict], Built]] = {
    "iron_condor": _iron_condor, "iron_fly": _iron_fly,
    "bull_put_spread": _bull_put, "bear_call_spread": _bear_call,
    "bull_call_debit": _bull_call_debit, "bear_put_debit": _bear_put_debit,
    "long_straddle": _long_straddle, "long_strangle": _long_strangle,
    "long_call": _long_call, "long_put": _long_put,
}
_BUILDERS.update(STRATEGY_BUILDERS)               # the ten OptionStrategy classes, templates prefixed "os_"
TEMPLATE_DIRECTION = {"bull_put_spread": 1, "bull_call_debit": 1, "long_call": 1,
                      "bear_call_spread": -1, "bear_put_debit": -1, "long_put": -1, **STRATEGY_DIRECTION}


# ------------------------------------------------------------------ library
def _make_id(template: str, params: tuple, regimes: tuple, band: tuple[int, int],
             trend: str, sl: float, tp: float, oi: str = "none", cost: float = 0.0) -> str:
    ptxt = "_".join(f"{k}{v:g}" for k, v in params)
    rtxt = "".join(r[0] for r in regimes)
    base = f"{template}|{ptxt}|r{rtxt}|d{band[0]}-{band[1]}|t{trend}|x{sl:g}-{tp:g}"
    base = base if cost <= 0 else f"{base}|c{round(cost * 100)}"
    return base if oi == "none" else f"{base}|oi={oi}"   # plain ids stay unchanged (approvals keep working)


def build_library() -> list[StrategySpec]:
    out: list[StrategySpec] = []

    def add(template: str, params: dict, regimes: tuple, band: tuple[int, int],
            trend: str = "none", sl: float = 0.6, tp: float = 0.6, oi: str = "none", cost: float = 0.0) -> None:
        pt = tuple(params.items())
        out.append(StrategySpec(_make_id(template, pt, regimes, band, trend, sl, tp, oi, cost),
                                template, pt, regimes, band[0], band[1], trend, sl_frac=sl, tp_frac=tp,
                                oi_rule=oi, max_cost_frac=cost))

    neutral_regimes = ((LOW,), (LOW, NORM))
    for band in DTE_BANDS:
        for regimes in neutral_regimes:
            for delta in (0.10, 0.15, 0.20):
                for wing in (500, 1000, 1500):
                    add("iron_condor", {"delta": delta, "wing": wing}, regimes, band)
            for wing in (500, 1000, 1500):
                for trend in ("none", "flat"):
                    add("iron_fly", {"wing": wing}, regimes, band, trend)
        for delta in (0.15, 0.25, 0.30):
            for width in (200, 500, 1000):                 # 200 = fits small accounts (about Rs 3.3k max loss/lot)
                for trend in ("none", "with"):
                    add("bull_put_spread", {"delta": delta, "width": width}, (LOW, NORM), band, trend)
                    add("bear_call_spread", {"delta": delta, "width": width}, (LOW, NORM), band, trend)
        for width in (200, 300, 500, 1000):                # 200/300 = about Rs 2k / 2.9k max loss per lot
            for regimes in ((HIGH,), (NORM, HIGH)):
                for sl, tp in ((0.6, 0.6), (0.5, 1.0)):
                    add("bull_call_debit", {"width": width}, regimes, band, "with", sl, tp)
                    add("bear_put_debit", {"width": width}, regimes, band, "with", sl, tp)
        for regimes in ((NORM, HIGH), (HIGH,)):
            for tp_mult in (0.5, 1.0):
                add("long_straddle", {"tp_mult": tp_mult}, regimes, band, "none", 0.5, 1.0)
                for delta in (0.25, 0.35):
                    add("long_strangle", {"delta": delta, "tp_mult": tp_mult}, regimes, band, "none", 0.5, 1.0)
    # ---- OI / gamma-rule variants: same trades as plain twins, plus a live-data filter. Not backtestable.
    for band in DTE_BANDS:
        for regimes in neutral_regimes:
            for delta in (0.15, 0.20):
                for wing in (500, 1000):
                    for rule in ("walls", "pcr_calm", "gex_pos"):
                        add("iron_condor", {"delta": delta, "wing": wing}, regimes, band, oi=rule)
        for delta in (0.15, 0.25):
            for width in (500, 1000):
                for rule in ("walls", "flow"):
                    add("bull_put_spread", {"delta": delta, "width": width}, (LOW, NORM), band, oi=rule)
                    add("bear_call_spread", {"delta": delta, "width": width}, (LOW, NORM), band, oi=rule)
        for regimes in ((NORM, HIGH), (HIGH,)):
            for tp_mult in (0.5, 1.0):
                add("long_straddle", {"tp_mult": tp_mult}, regimes, band, "none", 0.5, 1.0, oi="gex_neg")
                for delta in (0.25, 0.35):
                    add("long_strangle", {"delta": delta, "tp_mult": tp_mult}, regimes, band,
                        "none", 0.5, 1.0, oi="gex_neg")
    # ---- small-account family: narrow (200/300-point) defined-risk structures, worst case about Rs 1.8k-5k per lot.
    # More parameter combinations are MORE candidates, not more edge: each one still has to pass the backtest and
    # the forward shadow gates, and the more that are tried the more some pass by luck.
    seen = {s.id for s in out}
    before = len(out)

    def add_new(template: str, params: dict, regimes: tuple, band: tuple[int, int],
                trend: str, sl: float, tp: float) -> None:
        if _make_id(template, tuple(params.items()), regimes, band, trend, sl, tp) not in seen:
            add(template, params, regimes, band, trend, sl, tp)
            seen.add(out[-1].id)

    for band in DTE_BANDS:
        for width in (200, 300):
            for delta in (0.20, 0.25, 0.30):
                for trend in ("none", "with"):
                    for sl, tp in ((0.6, 0.6), (0.5, 0.4), (0.8, 0.5)):
                        add_new("bull_put_spread", {"delta": delta, "width": width}, (LOW, NORM), band, trend, sl, tp)
                        add_new("bear_call_spread", {"delta": delta, "width": width}, (LOW, NORM), band, trend, sl, tp)
        for regimes in ((LOW,), (LOW, NORM)):
            for wing in (200, 300):
                for delta in (0.15, 0.20):
                    for sl, tp in ((0.6, 0.6), (0.5, 0.4)):
                        add_new("iron_condor", {"delta": delta, "wing": wing}, regimes, band, "none", sl, tp)
        for width in (200, 300):
            for regimes in ((HIGH,), (NORM, HIGH)):
                for sl, tp in ((0.5, 0.7), (0.7, 0.5)):
                    add_new("bull_call_debit", {"width": width}, regimes, band, "with", sl, tp)
                    add_new("bear_put_debit", {"width": width}, regimes, band, "with", sl, tp)
        for width in (200, 300):                           # nearer-the-money credit spreads, trend-aligned
            add_new("bull_put_spread", {"delta": 0.35, "width": width}, (LOW, NORM), band, "with", 0.6, 0.6)
            add_new("bear_call_spread", {"delta": 0.35, "width": width}, (LOW, NORM), band, "with", 0.6, 0.6)
    SMALL_FAMILY_SIZE = len(out) - before
    if SMALL_FAMILY_SIZE != 200:                       # informational: editing the lists changes the family size
        log.warning("Small-account family has %d variants (expected 200)", SMALL_FAMILY_SIZE)
    # ---- trend / trap family: trade WITH a clean one-directional move ("valid") or AGAINST a failed breakout
    # ("trap"). d* use daily bars (backtestable); i* use 5-minute bars (forward-validated only). Widths 200-500 keep
    # them affordable for small accounts; capital tiers are evaluated separately.
    for band in DTE_BANDS:
        for mode in ("dvalid", "dtrap", "ivalid", "itrap"):
            for delta in (0.25, 0.35):
                for width in (200, 500):
                    add("bull_put_spread", {"delta": delta, "width": width}, (LOW, NORM), band, mode)
                    add("bear_call_spread", {"delta": delta, "width": width}, (LOW, NORM), band, mode)
            for width in (200, 300, 500):
                for regimes in ((NORM, HIGH), (HIGH,)):
                    add("bull_call_debit", {"width": width}, regimes, band, mode, 0.5, 1.0)
                    add("bear_put_debit", {"width": width}, regimes, band, mode, 0.5, 1.0)
    # ---- low-cost directional family for small accounts: fewer legs (less brokerage) and a cost-vs-target filter.
    # Entered only on a valid trend or a trap; skipped when round-trip costs would eat > 20% of the target profit.
    for band in DTE_BANDS:
        for mode in ("dvalid", "dtrap", "ivalid", "itrap"):
            for regimes in ((NORM, HIGH), (LOW, NORM, HIGH)):
                for delta in (0.20, 0.30):
                    for tp_mult in (1.0, 1.5):
                        add("long_call", {"delta": delta, "tp_mult": tp_mult}, regimes, band, mode, 0.5, 1.0, cost=0.20)
                        add("long_put", {"delta": delta, "tp_mult": tp_mult}, regimes, band, mode, 0.5, 1.0, cost=0.20)
                for width in (200, 300):
                    add("bull_call_debit", {"width": width}, regimes, band, mode, 0.5, 1.0, cost=0.20)
                    add("bear_put_debit", {"width": width}, regimes, band, mode, 0.5, 1.0, cost=0.20)
    # ---- the ten OptionStrategy classes (option_strategies.py): offsets/widths in index points. Directional ones
    # trade only with the trend ("with") or a valid daily trend ("dvalid"); neutral ones when the market is calm.
    NH, H = (NORM, HIGH), (HIGH,)
    for band in DTE_BANDS:
        for width in (200, 300, 500):
            for regimes in (NH, H):
                for trend in ("with", "dvalid"):
                    add("os_bull_call_spread", {"long_offset": 0, "width": width}, regimes, band, trend, 0.5, 0.5)
                    add("os_bear_put_spread", {"long_offset": 0, "width": width}, regimes, band, trend, 0.5, 0.5)
        for off in (200, 400):
            for width in (200, 500):
                for trend in ("with", "none"):
                    add("os_bull_put_spread", {"short_offset": off, "width": width}, (LOW, NORM), band, trend, 0.6, 0.8)
                    add("os_bear_call_spread", {"short_offset": off, "width": width}, (LOW, NORM), band, trend, 0.6, 0.8)
        for w1, w2, w3 in ((200, 200, 100), (300, 200, 200), (300, 300, 200), (500, 300, 300)):
            for trend in ("with", "dvalid"):
                add("os_bear_put_ladder", {"long_offset": 0, "w1": w1, "w2": w2, "w3": w3}, NH, band, trend, 0.5, 0.5)
        for w1, w2 in ((200, 300), (300, 400), (500, 500)):
            for trend in ("with", "dvalid"):
                add("os_bull_call_ratio", {"long_offset": 0, "w1": w1, "w2": w2}, (LOW, NORM), band, trend, 0.6, 0.5)
        for off in (600, 900):
            for wing in (300, 500, 1000):
                for regimes in ((LOW,), (LOW, NORM)):
                    add("os_iron_condor", {"short_offset": off, "wing": wing}, regimes, band, "none", 0.6, 0.8)
        for wing in (300, 500, 1000):
            for regimes in ((LOW,), (LOW, NORM)):
                add("os_iron_butterfly", {"wing": wing}, regimes, band, "none", 0.6, 0.8)
        for itm in (100, 200):
            for width in (400, 600):
                for regimes in (NH, H):
                    add("os_bull_call_spread_itm", {"itm_offset": itm, "width": width}, regimes, band, "with", 0.5, 0.5)
                    add("os_bear_put_spread_itm", {"itm_offset": itm, "width": width}, regimes, band, "with", 0.5, 0.5)
    # ---- intraday price-action family (intraday.py), validated by intraday_backtest.py on 5-minute history.
    # sig: 1 breakout, 2 trap (failed breakout), 3 range day. dir: 0 both, -1 bearish only. flt: sideways-day filters.
    # ib_long (one option) is the single-leg family for small accounts; the rest are multi-leg.
    ALL = (LOW, NORM, HIGH)
    for flt in (0, 1):
        for d in (0, -1):
            for conf in (0, 1):
                for tr in (1.5, 2.5):
                    for otm in (0, 1):
                        for be in (0, 1):
                            add("ib_long", {"sig": 1, "dir": d, "flt": flt, "conf": conf, "tr": tr, "otm": otm,
                                            "be": be}, ALL, (1, 7), "none", 0.9, 1.0)
    for tr in (1.5, 2.5):
        for be in (0, 1):
            add("ib_long", {"sig": 2, "tr": tr, "otm": 0, "be": be}, ALL, (1, 7), "none", 0.9, 1.0)
    for flt in (0, 1):
        for d in (0, -1):
            for width in (200, 400):
                add("ib_debit", {"sig": 1, "dir": d, "flt": flt, "width": width, "tr": 2.0, "be": 1},
                    ALL, (1, 7), "none", 0.9, 1.0)
            for width in (200, 500):
                for off in (0, 100):
                    add("ib_credit", {"sig": 1, "dir": d, "flt": flt, "width": width, "off": off},
                        ALL, (1, 7), "none", 0.9, 1.0)
    for width in (200, 500):
        add("ib_credit", {"sig": 2, "width": width, "off": 0}, ALL, (1, 7), "none", 0.9, 1.0)
    for width in (300, 500):
        for off in (0, 100):
            add("ib_condor", {"sig": 3, "width": width, "off": off}, ALL, (1, 7), "none", 0.9, 1.0)
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):                      # explicit raise: asserts vanish under python -O
        raise ValueError("duplicate strategy ids in the library")
    return out


# ------------------------------------------------------------------ planning
def _trend_ok(spec: StrategySpec, trend_dir: Optional[int], tinfo: Optional[TrendInfo] = None) -> bool:
    if spec.trend == "none":
        return True
    if spec.trend in ("dvalid", "dtrap", "ivalid", "itrap"):
        st = None if tinfo is None else (tinfo.daily if spec.trend[0] == "d" else tinfo.intraday)
        want = TEMPLATE_DIRECTION.get(spec.template, 0)
        if st is None or want == 0:
            return False                                   # fail closed without data
        return (st.valid and st.dir == want) if spec.trend.endswith("valid") else st.trap_dir == want
    if trend_dir is None:
        return False
    if spec.trend == "flat":
        return trend_dir == 0
    want = TEMPLATE_DIRECTION.get(spec.template, 0)   # "with"
    return want == 0 or trend_dir == want


def _oi_ok(spec: StrategySpec, legs: tuple[Leg, ...], f: Optional[ChainFeatures]) -> bool:
    """Evaluate the variant's OI/gamma rule. Fails closed when features are unavailable."""
    if spec.oi_rule == "none":
        return True
    if f is None:
        return False
    shorts_c = [l.contract.strike for l in legs if l.side == "SELL" and l.contract.kind == "CE"]
    shorts_p = [l.contract.strike for l in legs if l.side == "SELL" and l.contract.kind == "PE"]
    rule = spec.oi_rule
    if rule == "walls":
        # The biggest OI strikes must sit between spot and our short strikes, so price has to push
        # through the wall before it threatens the position.
        ok = bool(shorts_c or shorts_p)
        if shorts_c:
            ok = ok and f.call_wall is not None and f.call_wall <= min(shorts_c)
        if shorts_p:
            ok = ok and f.put_wall is not None and f.put_wall >= max(shorts_p)
        return ok
    if rule == "pcr_calm":
        return 0.8 <= f.pcr <= 1.3                 # NaN compares False -> fail closed
    if rule == "gex_pos":
        return f.net_gex > 0
    if rule == "gex_neg":
        return f.net_gex < 0
    if rule == "flow":
        if f.call_oi_chg is None or f.put_oi_chg is None:
            return False
        if spec.template == "bull_put_spread":     # put writing building faster than call writing
            return f.put_oi_chg > 0 and f.put_oi_chg > f.call_oi_chg
        if spec.template == "bear_call_spread":
            return f.call_oi_chg > 0 and f.call_oi_chg > f.put_oi_chg
        return False
    return False


def _round_trip_cost(legs: tuple[Leg, ...]) -> float:
    """Approximate charges for one lot: entry at the quoted price, exit at the same price."""
    from costs import CostParams, trade_cost
    q = legs[0].contract.lot_size
    opp = {"BUY": "SELL", "SELL": "BUY"}
    fills = [(l.side, l.ref_price, q * l.ratio) for l in legs] + [(opp[l.side], l.ref_price, q * l.ratio) for l in legs]
    return trade_cost(fills, CostParams())


def build_plan(spec: StrategySpec, chain: Chain, rs: RegimeState, trend_dir: Optional[int],
               today: dt.date, now: dt.datetime, features: Optional[ChainFeatures] = None,
               tinfo: Optional[TrendInfo] = None, ictx: Optional[IntradayContext] = None) -> Optional[TradePlan]:
    """Return a concrete trade for this variant right now, or None. Intraday-signal variants need `ictx`."""
    if rs.spike or rs.regime.value not in spec.regimes:
        return None
    if now.time() < spec.entry_after:
        return None
    if not (spec.dte_min <= (chain.expiry - today).days <= spec.dte_max):
        return None
    if spec.intraday_signal:
        sig = ictx.signal(spec) if ictx is not None else None
        if sig is None:
            return None
        built = _build_intraday(spec, chain, sig)
        if not built:
            return None
        legs, max_loss, max_profit, why = built
        if max_loss <= 0 or max_profit <= 0:
            return None
        lo, hi, be = _spot_bounds(spec, sig)
        return TradePlan(
            name=spec.template.upper(), regime=rs.regime.value, expiry=chain.expiry, legs=legs,
            lot_size=legs[0].contract.lot_size, max_loss_unit=max_loss, max_profit_unit=max_profit,
            rationale=why, variant_id=spec.id, sl_frac=spec.sl_frac, tp_frac=spec.tp_frac,
            spot_lo=lo, spot_hi=hi, spot_be=be, entry_spot=sig.entry)
    if not _trend_ok(spec, trend_dir, tinfo):
        return None
    built = _BUILDERS[spec.template](chain, spec.p)
    if not built:
        return None
    legs, max_loss, max_profit, why = built
    if max_loss <= 0 or max_profit <= 0:
        return None
    if not _oi_ok(spec, legs, features):
        return None
    if spec.max_cost_frac > 0 and _round_trip_cost(legs) > spec.max_cost_frac * spec.tp_frac * max_profit * legs[0].contract.lot_size:
        return None                                     # costs would eat too much of the target: not worth taking
    if spec.oi_rule != "none":
        why += f" [{spec.oi_rule}]"
    return TradePlan(
        name=spec.template.upper(), regime=rs.regime.value, expiry=chain.expiry, legs=legs,
        lot_size=legs[0].contract.lot_size, max_loss_unit=max_loss, max_profit_unit=max_profit,
        rationale=why, variant_id=spec.id, sl_frac=spec.sl_frac, tp_frac=spec.tp_frac)


# ------------------------------------------------------------------ approvals
def load_approved(path: str, today: dt.date, tier: Optional[str] = None) -> dict[str, float]:
    """Variant id -> score. Missing, malformed or expired approvals return {} (fail closed).

    With `tier` ("small" | "medium" | "large") only variants approved for that account size are returned. Entries
    that carry no `tiers` key (older files, forward promotions) stay eligible."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if dt.date.fromisoformat(doc["valid_until"]) < today:
            log.warning("Approvals in %s expired on %s", path, doc["valid_until"])
            return {}
        return {v["id"]: float(v.get("score", 0.0)) for v in doc["variants"]
                if tier is None or "tiers" not in v or tier in v["tiers"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
