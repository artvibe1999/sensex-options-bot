"""Object-oriented strategy engine: an `OptionStrategy` base class and the ten structures the bot supports.

Every strategy builds its own legs from a live (or modelled) option chain using user-set offsets in index points,
then derives its own theoretical economics at expiry: net premium, maximum risk, maximum profit and the upper/lower
breakevens. They come from a generic piecewise-linear payoff analysis, so ratio and ladder structures are handled
exactly and nothing is hand-derived per strategy. A structure with unbounded loss is rejected outright.

Strategies (registry key -> class)
   1  bull_call_spread       Bull Call Spread, directional debit
   2  bear_put_spread        Bear Put Spread, directional debit
   3  bull_put_spread       Bull Put Spread, credit
   4  bear_call_spread       Bear Call Spread, credit
   5  bear_put_ladder        Bear Put Ladder, risk-defined: long K1, short K2, short K3, long K4 (protective wing)
   6  iron_condor            Iron Condor, neutral credit
   7  iron_butterfly         Iron Butterfly, neutral credit
   8  bull_call_ratio        Bull Call Ratio Spread: long 1 K1, short 2 K2, long 1 far-OTM K3 (capped risk)
   9  bull_call_spread_itm   Bull Call Spread, pure debit (ITM long leg, debit capped as a share of the width)
  10  bear_put_spread_itm    Bear Put Spread, pure debit (ITM long leg, debit capped as a share of the width)

The strategies plug into the backtest, shadow book and live bot through `STRATEGY_BUILDERS` (see
strategy_library.py), which are registered as templates with the prefix `os_`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from strategy_engine import Chain, Leg

BUY, SELL = "BUY", "SELL"
Built = Optional[tuple[tuple[Leg, ...], float, float, str]]


# ---------------------------------------------------------------- data types
@dataclass(frozen=True)
class LegSpec:
    strike: float
    kind: str            # CE / PE
    side: str            # BUY / SELL
    ratio: int = 1


@dataclass(frozen=True)
class ExitRules:
    """Per-strategy exit triggers. Fractions are of the structure's theoretical maximum profit / loss, per lot."""
    tp_frac: float       # take profit: 0.5 for debit structures, 0.8 for credit structures (80% decay)
    sl_frac: float       # hard stop as a share of maximum loss
    time_exit: str = "square_off"   # the schedule's square-off time (earlier on expiry day): never holds into expiry


@dataclass(frozen=True)
class Economics:
    net_premium: float           # per unit; positive = net debit paid, negative = net credit received
    max_loss: float              # per unit, positive number
    max_profit: float            # per unit
    breakevens: tuple            # index levels at expiry where P&L crosses zero (ascending)


@dataclass(frozen=True)
class EntryContext:
    regime: str                  # LOW_VOL / NORMAL / HIGH_VOL
    trend_dir: Optional[int]     # +1 / -1 / 0 / None (EMA trend on completed daily closes, as in the backtest)
    spike: bool = False


# ------------------------------------------------------------- payoff maths
def _intrinsic(kind: str, strike: float, s: float) -> float:
    return max(s - strike, 0.0) if kind == "CE" else max(strike - s, 0.0)


def expiry_payoff(legs: Sequence[tuple[str, float, str, int, float]], s: float) -> float:
    """Per-unit P&L at expiry. legs: (kind, strike, side, ratio, entry_price)."""
    total = 0.0
    for kind, strike, side, ratio, px in legs:
        sign = 1 if side == BUY else -1
        total += sign * ratio * (_intrinsic(kind, strike, s) - px)
    return total


def analyse(legs: Sequence[tuple[str, float, str, int, float]]) -> Optional[Economics]:
    """Max loss/profit and breakevens of a multi-leg expiry payoff. None when the downside is unbounded."""
    strikes = sorted({k for _, k, _, _, _ in legs})
    top = strikes[-1] * 2.0
    up_slope = sum((1 if side == BUY else -1) * ratio for kind, _, side, ratio, _ in legs if kind == "CE")
    if up_slope < 0:
        return None                                   # naked short calls beyond the last strike: unbounded loss
    pts = [0.0] + strikes + [top]
    vals = [expiry_payoff(legs, p) for p in pts]
    max_profit = max(vals)          # profit is capped in every shipped strategy (a positive far slope is cut at 2x top)
    max_loss = -min(vals)
    bes: list[float] = []
    for (x0, y0), (x1, y1) in zip(zip(pts, vals), zip(pts[1:], vals[1:])):
        if y0 == 0 and 0 < x0 < top:
            bes.append(x0)
        elif y0 * y1 < 0:
            bes.append(x0 + (0 - y0) * (x1 - x0) / (y1 - y0))
    if up_slope > 0 and vals[-1] < 0:                 # loss beyond the last point would recover further out
        bes.append(top + (-vals[-1]) / up_slope)
    net = sum((1 if side == BUY else -1) * ratio * px for _, _, side, ratio, px in legs)
    return Economics(round(net, 2), round(max(max_loss, 0.0), 2), round(max(max_profit, 0.0), 2),
                     tuple(sorted(round(b, 1) for b in set(bes))))


# ---------------------------------------------------------------- base class
class OptionStrategy(ABC):
    key: str = ""
    label: str = ""
    category: str = ""           # debit | credit | neutral
    direction: int = 0           # +1 bullish, -1 bearish, 0 neutral
    regimes: tuple = ("LOW_VOL", "NORMAL")
    min_reward_risk: float = 0.0
    exit_rules: ExitRules = ExitRules(0.6, 0.6)
    defaults: dict = {}

    def __init__(self, **params) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.key}: unknown parameters {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    # -- strike selection -------------------------------------------------------
    @abstractmethod
    def select_strikes(self, chain: Chain) -> Optional[list[LegSpec]]:
        """Pick the strikes for this structure from the chain, or None when they are not all listed."""

    @staticmethod
    def at(chain: Chain, offset: float) -> float:
        """Strike `offset` index points from ATM, snapped to the listed strike grid."""
        return chain.atm + round(offset / chain.step) * chain.step

    # -- construction ------------------------------------------------------------
    def build(self, chain: Chain) -> Optional[tuple[tuple[Leg, ...], Economics]]:
        specs = self.select_strikes(chain)
        if not specs:
            return None
        legs, priced = [], []
        for sp in specs:
            q = chain.get(sp.strike, sp.kind)
            if q is None:
                return None                               # unquoted or zero-priced: fail closed
            legs.append(Leg(q.contract, sp.side, q.ltp, sp.ratio))
            priced.append((sp.kind, sp.strike, sp.side, sp.ratio, q.ltp))
        eco = analyse(priced)
        if eco is None or eco.max_loss <= 0 or eco.max_profit <= 0:
            return None
        if eco.max_profit / eco.max_loss < self.min_reward_risk:
            return None
        if not self.accept(eco, specs):
            return None
        return tuple(legs), eco

    def accept(self, eco: Economics, specs: list[LegSpec]) -> bool:
        """Strategy-specific quality gate on the priced structure."""
        return True

    # -- triggers ------------------------------------------------------------------
    def entry_trigger(self, ctx: EntryContext) -> bool:
        """Dynamic entry condition: volatility regime and market direction must suit the structure."""
        if ctx.spike or ctx.regime not in self.regimes:
            return False
        if self.direction == 0:
            return ctx.trend_dir in (0, None)
        return ctx.trend_dir == self.direction

    def describe(self, eco: Economics) -> str:
        be = "/".join(f"{b:.0f}" for b in eco.breakevens) or "-"
        kind = "debit" if eco.net_premium > 0 else "credit"
        return (f"{self.label}: {kind} {abs(eco.net_premium):.1f}, max loss {eco.max_loss:.1f}, "
                f"max profit {eco.max_profit:.1f}, breakevens {be}")

    def as_builder(self) -> Callable[[Chain, dict], Built]:
        """Adapter to the strategy library's builder signature (legs, max_loss, max_profit, rationale)."""
        def _builder(chain: Chain, params: dict) -> Built:
            built = type(self)(**{k: v for k, v in params.items() if k in self.defaults}).build(chain)
            if not built:
                return None
            legs, eco = built
            return legs, eco.max_loss, eco.max_profit, self.describe(eco)
        return _builder


# ---------------------------------------------------------- the 10 strategies
class BullCallSpread(OptionStrategy):
    key, label, category, direction = "bull_call_spread", "Bull Call Spread", "debit", 1
    regimes = ("NORMAL", "HIGH_VOL")
    min_reward_risk = 0.8
    exit_rules = ExitRules(tp_frac=0.5, sl_frac=0.5)
    defaults = {"long_offset": 0, "width": 300}

    def select_strikes(self, chain):
        k1 = self.at(chain, self.params["long_offset"])
        return [LegSpec(k1, "CE", BUY), LegSpec(k1 + self.params["width"], "CE", SELL)]


class BearPutSpread(OptionStrategy):
    key, label, category, direction = "bear_put_spread", "Bear Put Spread", "debit", -1
    regimes = ("NORMAL", "HIGH_VOL")
    min_reward_risk = 0.8
    exit_rules = ExitRules(tp_frac=0.5, sl_frac=0.5)
    defaults = {"long_offset": 0, "width": 300}

    def select_strikes(self, chain):
        k1 = self.at(chain, -self.params["long_offset"])
        return [LegSpec(k1, "PE", BUY), LegSpec(k1 - self.params["width"], "PE", SELL)]


class BullPutSpread(OptionStrategy):
    key, label, category, direction = "bull_put_spread", "Bull Put Spread", "credit", 1
    regimes = ("LOW_VOL", "NORMAL")
    exit_rules = ExitRules(tp_frac=0.8, sl_frac=0.6)
    defaults = {"short_offset": 300, "width": 300}

    def select_strikes(self, chain):
        ks = self.at(chain, -self.params["short_offset"])
        return [LegSpec(ks, "PE", SELL), LegSpec(ks - self.params["width"], "PE", BUY)]

    def accept(self, eco, specs):
        return -eco.net_premium >= 0.15 * self.params["width"]          # collect at least 15% of the width


class BearCallSpread(OptionStrategy):
    key, label, category, direction = "bear_call_spread", "Bear Call Spread", "credit", -1
    regimes = ("LOW_VOL", "NORMAL")
    exit_rules = ExitRules(tp_frac=0.8, sl_frac=0.6)
    defaults = {"short_offset": 300, "width": 300}

    def select_strikes(self, chain):
        ks = self.at(chain, self.params["short_offset"])
        return [LegSpec(ks, "CE", SELL), LegSpec(ks + self.params["width"], "CE", BUY)]

    def accept(self, eco, specs):
        return -eco.net_premium >= 0.15 * self.params["width"]


class BearPutLadder(OptionStrategy):
    """Long K1 put, short K2 put, short K3 put, long K4 put: the K4 wing removes the naked-short-put tail."""
    key, label, category, direction = "bear_put_ladder", "Bear Put Ladder (risk-defined)", "debit", -1
    regimes = ("NORMAL", "HIGH_VOL")
    exit_rules = ExitRules(tp_frac=0.5, sl_frac=0.5)
    defaults = {"long_offset": 0, "w1": 300, "w2": 200, "w3": 200}

    def select_strikes(self, chain):
        p = self.params
        k1 = self.at(chain, -p["long_offset"])
        k2, k3 = k1 - p["w1"], k1 - p["w1"] - p["w2"]
        return [LegSpec(k1, "PE", BUY), LegSpec(k2, "PE", SELL), LegSpec(k3, "PE", SELL),
                LegSpec(k3 - p["w3"], "PE", BUY)]


class IronCondor(OptionStrategy):
    key, label, category, direction = "iron_condor", "Iron Condor", "neutral", 0
    regimes = ("LOW_VOL", "NORMAL")
    exit_rules = ExitRules(tp_frac=0.8, sl_frac=0.6)
    defaults = {"short_offset": 800, "wing": 500}

    def select_strikes(self, chain):
        o, w = self.params["short_offset"], self.params["wing"]
        kc, kp = self.at(chain, o), self.at(chain, -o)
        return [LegSpec(kc, "CE", SELL), LegSpec(kc + w, "CE", BUY),
                LegSpec(kp, "PE", SELL), LegSpec(kp - w, "PE", BUY)]

    def accept(self, eco, specs):
        return -eco.net_premium >= 0.10 * self.params["wing"]


class IronButterfly(OptionStrategy):
    key, label, category, direction = "iron_butterfly", "Iron Butterfly", "neutral", 0
    regimes = ("LOW_VOL", "NORMAL")
    exit_rules = ExitRules(tp_frac=0.8, sl_frac=0.6)
    defaults = {"wing": 500}

    def select_strikes(self, chain):
        k, w = chain.atm, self.params["wing"]
        return [LegSpec(k, "CE", SELL), LegSpec(k, "PE", SELL),
                LegSpec(k + w, "CE", BUY), LegSpec(k - w, "PE", BUY)]

    def accept(self, eco, specs):
        return -eco.net_premium >= 0.15 * self.params["wing"]


class BullCallRatioSpread(OptionStrategy):
    """Long 1 K1 call, short 2 K2 calls, long 1 far-OTM K3 call: the K3 wing caps the loss above K3."""
    key, label, category, direction = "bull_call_ratio", "Bull Call Ratio Spread (protected)", "debit", 1
    regimes = ("LOW_VOL", "NORMAL")
    exit_rules = ExitRules(tp_frac=0.5, sl_frac=0.6)
    defaults = {"long_offset": 0, "w1": 300, "w2": 400}

    def select_strikes(self, chain):
        p = self.params
        k1 = self.at(chain, p["long_offset"])
        k2 = k1 + p["w1"]
        return [LegSpec(k1, "CE", BUY), LegSpec(k2, "CE", SELL, 2), LegSpec(k2 + p["w2"], "CE", BUY)]


class BullCallSpreadPureDebit(OptionStrategy):
    key, label, category, direction = "bull_call_spread_itm", "Bull Call Spread (pure debit, ITM long)", "debit", 1
    regimes = ("NORMAL", "HIGH_VOL")
    min_reward_risk = 0.8
    exit_rules = ExitRules(tp_frac=0.5, sl_frac=0.5)
    defaults = {"itm_offset": 100, "width": 500}

    def select_strikes(self, chain):
        k1 = self.at(chain, -self.params["itm_offset"])
        return [LegSpec(k1, "CE", BUY), LegSpec(k1 + self.params["width"], "CE", SELL)]

    def accept(self, eco, specs):
        return 0 < eco.net_premium <= 0.55 * self.params["width"]        # a true debit, not an over-paid one


class BearPutSpreadPureDebit(OptionStrategy):
    key, label, category, direction = "bear_put_spread_itm", "Bear Put Spread (pure debit, ITM long)", "debit", -1
    regimes = ("NORMAL", "HIGH_VOL")
    min_reward_risk = 0.8
    exit_rules = ExitRules(tp_frac=0.5, sl_frac=0.5)
    defaults = {"itm_offset": 100, "width": 500}

    def select_strikes(self, chain):
        k1 = self.at(chain, self.params["itm_offset"])
        return [LegSpec(k1, "PE", BUY), LegSpec(k1 - self.params["width"], "PE", SELL)]

    def accept(self, eco, specs):
        return 0 < eco.net_premium <= 0.55 * self.params["width"]


STRATEGY_CLASSES: dict[str, type[OptionStrategy]] = {c.key: c for c in (
    BullCallSpread, BearPutSpread, BullPutSpread, BearCallSpread, BearPutLadder, IronCondor,
    IronButterfly, BullCallRatioSpread, BullCallSpreadPureDebit, BearPutSpreadPureDebit)}

TEMPLATE_PREFIX = "os_"
STRATEGY_BUILDERS: dict[str, Callable[[Chain, dict], Built]] = {
    TEMPLATE_PREFIX + k: c().as_builder() for k, c in STRATEGY_CLASSES.items()}
STRATEGY_DIRECTION: dict[str, int] = {TEMPLATE_PREFIX + k: c.direction for k, c in STRATEGY_CLASSES.items()
                                      if c.direction}
