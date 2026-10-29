"""Opportunity scanner: turns one market snapshot into a ranked list of explained setups.

Each setup is a checklist of independent conditions drawn from trend/trap state, volatility regime, option-chain
structure (OI walls, PCR, gamma, max pain) and liquidity. The score is the share of weighted conditions that hold
(0-100). It is a RANKING aid, not a probability: whether a high score actually wins is measured by the forward
shadow book, and the scan is stored (table `scan`) so that can be checked later.

Setups
  TREND_UP / TREND_DOWN      valid one-directional move, aligned with flow and not pinned against a wall
  TRAP_REVERSAL_UP / _DOWN   failed breakout (bear trap -> up, bull trap -> down) with room to run
  RANGE_PIN                  calm, positive-gamma, spot inside wide OI walls -> sell premium (defined risk)
  VOL_EXPANSION              negative gamma, rising/high vol, no clear direction -> buy premium
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy_engine import Regime

TEMPLATES = {          # setup -> library templates that express it
    "TREND_UP": ("bull_put_spread", "bull_call_debit", "long_call"),
    "TREND_DOWN": ("bear_call_spread", "bear_put_debit", "long_put"),
    "TRAP_REVERSAL_UP": ("bull_put_spread", "bull_call_debit", "long_call"),
    "TRAP_REVERSAL_DOWN": ("bear_call_spread", "bear_put_debit", "long_put"),
    "RANGE_PIN": ("iron_condor", "iron_fly"),
    "VOL_EXPANSION": ("long_straddle", "long_strangle"),
}
BIAS = {"TREND_UP": 1, "TREND_DOWN": -1, "TRAP_REVERSAL_UP": 1, "TRAP_REVERSAL_DOWN": -1,
        "RANGE_PIN": 0, "VOL_EXPANSION": 0}


@dataclass(frozen=True)
class Opportunity:
    setup: str
    bias: int
    score: float                              # 0-100, share of weighted conditions that hold
    passed: tuple = ()
    failed: tuple = ()
    templates: tuple = ()

    @property
    def evidence(self) -> str:
        return "+".join(self.passed) + (" | missing: " + ",".join(self.failed) if self.failed else "")


def _score(setup: str, checks: list[tuple[str, float, bool]]) -> Opportunity:
    tot = sum(w for _, w, _ in checks)
    got = sum(w for _, w, ok in checks if ok)
    return Opportunity(setup, BIAS[setup], round(100 * got / tot, 1) if tot else 0.0,
                       tuple(n for n, _, ok in checks if ok), tuple(n for n, _, ok in checks if not ok),
                       TEMPLATES[setup])


def liquid(snap) -> bool:
    f = snap.features
    return bool(f and f.atm_spread_pct is not None and f.atm_spread_pct <= 4.0)


def scan(snap, min_score: float = 55.0) -> list[Opportunity]:
    """Ranked opportunities (best first). Empty when data is missing, the market is spiking or the chain is illiquid:
    no data or bad liquidity is never an opportunity."""
    if snap is None or snap.chain is None or snap.features is None or snap.rs.spike or not liquid(snap):
        return []
    f, ti, rs, spot = snap.features, snap.tinfo, snap.rs, snap.spot
    d, i = (ti.daily, ti.intraday) if ti else (None, None)
    room = lambda lvl, sign: lvl is None or sign * (lvl - spot) / spot > 0.004      # >0.4% to the wall in our direction
    pcr_up, pcr_dn = f.pcr > 1.0, f.pcr < 0.8
    out: list[Opportunity] = []

    for setup, sign in (("TREND_UP", 1), ("TREND_DOWN", -1)):
        checks = [
            ("daily_trend", 2, bool(d and d.valid and d.dir == sign)),
            ("intraday_trend", 3, bool(i and i.valid and i.dir == sign)),
            ("no_opposing_trap", 1, not ((d and d.trap_dir == -sign) or (i and i.trap_dir == -sign))),
            ("room_to_wall", 2, room(f.call_wall if sign > 0 else f.put_wall, sign)),
            ("pcr_support", 1, pcr_up if sign > 0 else pcr_dn),
            ("not_calm_range", 1, rs.regime != Regime.LOW_VOL or bool(i and i.valid)),
        ]
        out.append(_score(setup, checks))

    for setup, sign in (("TRAP_REVERSAL_UP", 1), ("TRAP_REVERSAL_DOWN", -1)):
        checks = [
            ("intraday_trap", 3, bool(i and i.trap_dir == sign)),
            ("daily_trap", 2, bool(d and d.trap_dir == sign)),
            ("room_to_wall", 2, room(f.call_wall if sign > 0 else f.put_wall, sign)),
            ("flow_confirms", 1, (f.put_oi_chg or 0) > (f.call_oi_chg or 0) if sign > 0
             else (f.call_oi_chg or 0) > (f.put_oi_chg or 0)),
            ("pcr_support", 1, pcr_up if sign > 0 else pcr_dn),
        ]
        out.append(_score(setup, checks))

    # Neutral setups score on the ABSENCE of a trend/trap. Until the intraday read has enough bars (~70 min) that
    # absence is unknown, not a range: without this RANGE_PIN scored 100 at the 09:30 open every day.
    if i is not None and i.ready:
        inside = (f.put_wall is not None and f.call_wall is not None and f.put_wall < spot < f.call_wall
                  and (f.call_wall - f.put_wall) / spot > 0.02)
        out.append(_score("RANGE_PIN", [
            ("calm_regime", 2, rs.regime in (Regime.LOW_VOL, Regime.NORMAL)),
            ("positive_gamma", 3, f.net_gex > 0),
            ("inside_wide_walls", 2, inside),
            ("no_valid_trend", 2, not ((d and d.valid) or i.valid)),
            ("no_trap", 1, not ((d and d.trap) or i.trap)),
        ]))

        out.append(_score("VOL_EXPANSION", [
            ("negative_gamma", 3, f.net_gex < 0),
            ("elevated_vol", 2, rs.regime != Regime.LOW_VOL),
            ("no_valid_trend", 1, not ((d and d.valid) or i.valid)),
            ("price_near_max_pain_or_wall", 1, any(x is not None and abs(x - spot) / spot < 0.004
                                                   for x in (f.max_pain, f.call_wall, f.put_wall))),
        ]))
    out = [o for o in out if o.score >= min_score]
    return sorted(out, key=lambda o: -o.score)


def affordable(opp: Opportunity, specs, capital: float, plan_cost_fn=None) -> list[str]:
    """Variant ids that express this setup and could fit the account (template + trend-mode match only; actual
    sizing is done by the risk manager at entry)."""
    modes = {"TREND_UP": ("dvalid", "ivalid", "with"), "TREND_DOWN": ("dvalid", "ivalid", "with"),
             "TRAP_REVERSAL_UP": ("dtrap", "itrap"), "TRAP_REVERSAL_DOWN": ("dtrap", "itrap"),
             "RANGE_PIN": ("none", "flat"), "VOL_EXPANSION": ("none",)}[opp.setup]
    return [s.id for s in specs if s.template in opp.templates and s.trend in modes and not s.needs_oi]
