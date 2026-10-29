"""Stateful exit engine for multi-leg positions: combined rupee stop, neutral-zone lock and a laddered trailing floor.

Rules (all on the COMBINED mark-to-market P&L of every leg, quantities already include the lot size):

  Levels are per structure: stop = min(hard_stop_rs, sl_frac x max loss), arm = min(arm_rs, arm_frac x max profit).

  1. HARD STOP     combined P&L <= -stop                                 -> exit "HARD_STOP"
  2. NEUTRAL ZONE  -stop < P&L < +arm                                    -> HOLD, nothing here may exit the trade
  3. TRAILING      once the peak P&L reaches +arm the trail is armed. The lock-in floor only ever moves UP:
                     floor = max(previous floor, arm_rs,
                                 peak x (1 - giveback),                  # percentage of peak profit
                                 ladder milestone - lag)                 # step-up floor at 3000, 4000, ...
                   P&L <= floor                                          -> exit "TRAILING_STOP"

`giveback` starts at `base_giveback` and is adapted every evaluation from market signals (see `_adapt`): reversal /
exhaustion, PCR momentum, max-pain drift, OI build-up, VIX change, theta window. Adaptation can tighten or widen the
band but, because the floor is a ratchet, it can never loosen a profit that is already locked.

Safety exits that are NOT noise (square-off time, daily loss kill-switch, shutdown, broker mismatch) are handled by
the caller and are deliberately outside this engine.

State lives in `pos.exit_state` (JSON-safe dict) so a restart resumes with the same floor and peak.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Optional

from market_context import ContextSignals

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        log.warning("Bad %s value; using %s", name, default)
        return default


@dataclass(frozen=True)
class ExitParams:
    enabled: bool = True
    hard_stop_rs: float = 2000.0          # combined stop CEILING; the variant's sl_frac x max loss applies if tighter
    arm_rs: float = 2000.0                # trailing arms at, and never exits below, this profit (a ceiling, see arm_frac)
    # Scale the arm level to the structure: arm = min(arm_rs, arm_frac x max profit). A fixed Rs 2000 is out of reach
    # for a 1-lot narrow spread (max profit ~Rs 1.8k), so the entry gate only let iron flies through, and a fly's
    # max loss (~Rs 1.9k) sits inside the Rs 2000 stop, so nothing but 15:15 ever closed it. 0 = fixed arm_rs.
    arm_frac: float = 0.35
    base_giveback: float = 0.40           # share of peak profit the trail may give back before adaptation
    min_giveback: float = 0.15
    max_giveback: float = 0.60
    ladder_step_rs: float = 1000.0        # milestones at arm_rs + k x step (3000, 4000, ...)
    ladder_lag_steps: int = 2             # floor sits this many steps behind the highest milestone reached
    adapt: bool = True
    retrace_pct: float = 0.20             # % of spot retraced from the favourable extreme = reversal warning
    exhaustion_move_pct: float = 0.15     # 5-min move in our favour ...
    exhaustion_turn_pct: float = 0.05     # ... followed by this much against us in the last ~90 s
    neutral_breakout_pct: float = 0.50    # delta-neutral structures: spot this far from entry = breakout risk
    pcr_mom_thr: float = 0.05
    max_pain_thr: float = 100.0           # index points
    vix_chg_thr_pct: float = 3.0
    theta_window_start: dt.time = dt.time(13, 30)
    late_window_start: dt.time = dt.time(14, 15)
    factor_min: float = 0.30              # bounds on the combined adaptation factor
    factor_max: float = 1.30
    require_reachable_arm: bool = True    # entry gate: plan's max profit must be able to cross arm_rs
    suppress_day_trail: bool = True       # the day-level trailing stop would exit inside the neutral zone

    @classmethod
    def from_env(cls) -> "ExitParams":
        d = cls()
        return cls(
            enabled=(os.getenv("EXIT_ENGINE", "1") or "1") != "0",
            hard_stop_rs=_env_float("POSITION_STOP_RS", d.hard_stop_rs),
            arm_rs=_env_float("TRAIL_MIN_PROFIT_RS", d.arm_rs),
            arm_frac=_env_float("TRAIL_ARM_FRAC", d.arm_frac),
            base_giveback=_env_float("TRAIL_GIVEBACK", d.base_giveback),
            min_giveback=_env_float("TRAIL_GIVEBACK_MIN", d.min_giveback),
            max_giveback=_env_float("TRAIL_GIVEBACK_MAX", d.max_giveback),
            ladder_step_rs=_env_float("TRAIL_LADDER_STEP_RS", d.ladder_step_rs),
            ladder_lag_steps=int(_env_float("TRAIL_LADDER_LAG", d.ladder_lag_steps)),
            adapt=(os.getenv("TRAIL_ADAPT", "1") or "1") != "0",
            require_reachable_arm=(os.getenv("REQUIRE_REACHABLE_ARM", "1") or "1") != "0")


@dataclass(frozen=True)
class ExitDecision:
    reason: Optional[str] = None          # HARD_STOP | TRAILING_STOP | None (hold)
    pnl: float = 0.0
    peak: float = 0.0
    floor: Optional[float] = None
    armed: bool = False
    giveback: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- position description
def describe_position(legs, chain, plan_name: str = "") -> dict:
    """Net directional / vega / theta exposure of the structure from the chain greeks.

    bias: +1 bullish, -1 bearish, 0 delta-neutral (condor, straddle, ...). vega/theta are signed totals
    (units x greeks); vega < 0 means short volatility. Falls back to the strategy name if greeks are unavailable."""
    net_delta = gross_delta = vega = theta = 0.0   # gross_delta kept for diagnostics
    have = False
    for l in legs:
        q = chain.get(l.contract.strike, l.contract.kind) if chain is not None else None
        g = getattr(q, "greeks", None)
        if g is None:
            continue
        have = True
        sign = 1.0 if l.side == "BUY" else -1.0
        net_delta += sign * l.qty * g.delta
        gross_delta += abs(l.qty * g.delta)
        vega += sign * l.qty * g.vega
        theta += sign * l.qty * g.theta
    n = plan_name.lower()
    by_name = 1 if "bull" in n else (-1 if "bear" in n else 0)
    if have:
        unit = min((l.qty for l in legs if l.qty > 0), default=0) or 1
        nd = net_delta / unit                          # net delta per unit of the smallest leg (a 1-lot equivalent)
        if abs(nd) >= 0.05:
            bias = 1 if nd > 0 else -1
        elif abs(nd) >= 0.02 and by_name and (nd > 0) == (by_name > 0):
            bias = by_name                             # weakly directional and the strategy name agrees
        else:
            bias = 0
    else:
        bias = by_name
    return {"bias": bias, "vega": vega, "theta": theta, "greeks": have}


class ExitEngine:
    def __init__(self, params: ExitParams, verbose: bool = True) -> None:
        """verbose=False logs at DEBUG only (the shadow book runs one engine state per variant)."""
        self.p = params
        self._info = log.info if verbose else log.debug
        self._warn = log.warning if verbose else log.debug

    # ------------------------------------------------------------------ levels
    def arm_for(self, max_profit_total: float) -> float:
        """Profit at which the trail arms for a structure with this maximum profit (all lots)."""
        p = self.p
        if p.arm_frac > 0 and max_profit_total > 0:
            return min(p.arm_rs, p.arm_frac * max_profit_total)
        return p.arm_rs

    def stop_for(self, max_loss_total: float, sl_frac: Optional[float]) -> float:
        """Combined stop distance in rupees (0 = no stop): the variant's own stop, capped by hard_stop_rs."""
        p = self.p
        own = sl_frac * max_loss_total if sl_frac and max_loss_total > 0 else 0.0
        if p.hard_stop_rs > 0 and own > 0:
            return min(p.hard_stop_rs, own)
        return p.hard_stop_rs if p.hard_stop_rs > 0 else own

    def _levels(self, pos, st: dict) -> tuple[float, float]:
        if "stop_rs" not in st:                    # positions saved before levels were stored
            st["stop_rs"] = self.stop_for(getattr(pos, "max_loss", 0.0), getattr(pos.plan, "sl_frac", None))
            st["arm_rs"] = self.arm_for(getattr(pos, "max_profit", 0.0))
        return st["stop_rs"], st["arm_rs"]

    # ------------------------------------------------------------------ state
    def init_state(self, pos, chain, spot: float) -> None:
        """Call once when the position opens (or when restoring one that has no exit state yet)."""
        desc = describe_position(pos.legs, chain, pos.plan.name)
        pos.exit_state = {"bias": desc["bias"], "vega": desc["vega"], "theta": desc["theta"],
                          "entry_spot": spot, "hi": spot, "lo": spot, "floor": None, "armed": False, "notes": []}
        stop, arm = self._levels(pos, pos.exit_state)
        self._info("EXIT-ENGINE init %s bias=%+d vega=%.0f theta=%.0f greeks=%s hard_stop=-%.0f arm=+%.0f",
                 pos.plan.variant_id, desc["bias"], desc["vega"], desc["theta"], desc["greeks"], stop, arm)

    def reachable(self, max_profit_total: float) -> bool:
        """Can a position with this maximum profit ever cross the arm level? (entry gate)"""
        return max_profit_total > self.arm_for(max_profit_total)

    # --------------------------------------------------------------- adaptation
    def _adapt(self, st: dict, pos, sig: Optional[ContextSignals], spot: Optional[float],
               now: dt.datetime) -> tuple[float, list[str]]:
        p = self.p
        gb, notes = p.base_giveback, []
        if not p.adapt:
            return gb, notes
        f = 1.0
        b = int(st.get("bias", 0))
        # theta window: decay is fastest into the close, and on expiry day from early afternoon
        days_left = (pos.plan.expiry - now.date()).days
        if now.time() >= p.theta_window_start and days_left <= 1:
            f *= 0.80
            notes.append("theta-window(expiry)")
        elif now.time() >= p.late_window_start:
            f *= 0.90
            notes.append("theta-window(late)")
        if spot:
            if b > 0 and st.get("hi"):
                ret = 100.0 * (st["hi"] - spot) / st["hi"]
                if ret >= p.retrace_pct:
                    f *= 0.60
                    notes.append(f"reversal:retrace {ret:.2f}% off high")
            elif b < 0 and st.get("lo"):
                ret = 100.0 * (spot - st["lo"]) / st["lo"]
                if ret >= p.retrace_pct:
                    f *= 0.60
                    notes.append(f"reversal:retrace {ret:.2f}% off low")
            elif b == 0 and st.get("entry_spot"):
                mv = 100.0 * abs(spot - st["entry_spot"]) / st["entry_spot"]
                if mv >= p.neutral_breakout_pct:
                    f *= 0.80
                    notes.append(f"breakout-risk {mv:.2f}% from entry")
        if sig is not None:
            if b != 0 and sig.spot_chg_pct is not None and sig.spot_chg_short_pct is not None:
                if (b * sig.spot_chg_pct >= p.exhaustion_move_pct and b * sig.spot_chg_short_pct <= -p.exhaustion_turn_pct):
                    f *= 0.70
                    notes.append("momentum-exhaustion")
            if b != 0 and sig.pcr_mom is not None:
                if b * sig.pcr_mom <= -p.pcr_mom_thr:
                    f *= 0.80
                    notes.append(f"pcr-momentum against ({sig.pcr_mom:+.2f})")
                elif b * sig.pcr_mom >= p.pcr_mom_thr:
                    f *= 1.10
                    notes.append(f"pcr-momentum with ({sig.pcr_mom:+.2f})")
            if b != 0 and sig.max_pain_drift is not None:
                if b * sig.max_pain_drift <= -p.max_pain_thr:
                    f *= 0.90
                    notes.append(f"max-pain drifting against ({sig.max_pain_drift:+.0f})")
                elif b * sig.max_pain_drift >= p.max_pain_thr:
                    f *= 1.05
                    notes.append(f"max-pain drifting with ({sig.max_pain_drift:+.0f})")
            if b != 0:
                if sig.buildup_bias == -b:
                    f *= 0.80
                    notes.append(f"oi-buildup against ({sig.buildup})")
                elif sig.buildup_bias == b:
                    f *= 1.10
                    notes.append(f"oi-buildup with ({sig.buildup})")
                if sig.write_bias == -b:
                    f *= 0.85
                    notes.append("option-writing against")
            if sig.vix_chg_pct is not None:
                v = float(st.get("vega", 0.0))
                if sig.vix_chg_pct >= p.vix_chg_thr_pct:
                    f *= 0.80 if v < 0 else 1.10
                    notes.append(f"vix up {sig.vix_chg_pct:+.1f}% ({'short' if v < 0 else 'long'} vega)")
                elif sig.vix_chg_pct <= -p.vix_chg_thr_pct:
                    f *= 0.85 if v > 0 else 1.05
                    notes.append(f"vix down {sig.vix_chg_pct:+.1f}% ({'long' if v > 0 else 'short'} vega)")
        f = min(max(f, p.factor_min), p.factor_max)
        return min(max(gb * f, p.min_giveback), p.max_giveback), notes

    def _spot_exit(self, pos, st: dict, pnl: float, peak: float, spot: Optional[float]) -> ExitDecision:
        """Signal trades (intraday.py) exit on the UNDERLYING, exactly as intraday_backtest.py simulates them: at or
        beyond spot_lo / spot_hi, with the adverse bound moved to the entry level once spot_be is reached. The
        rupee trail does not apply (the backtest validated these exits, not the trail)."""
        plan = pos.plan
        if "spot_lo" not in st:
            st["spot_lo"], st["spot_hi"], st["spot_be"] = plan.spot_lo, plan.spot_hi, plan.spot_be
        if spot is None:
            return ExitDecision(None, pnl, peak)
        lo, hi, be, e = st["spot_lo"], st["spot_hi"], st["spot_be"], plan.entry_spot
        if lo is not None and spot <= lo:
            self._warn("EXIT-ENGINE SPOT_LO %s spot=%.1f <= %.1f pnl=%.0f", plan.variant_id, spot, lo, pnl)
            return ExitDecision("SPOT_LO", pnl, peak)
        if hi is not None and spot >= hi:
            self._warn("EXIT-ENGINE SPOT_HI %s spot=%.1f >= %.1f pnl=%.0f", plan.variant_id, spot, hi, pnl)
            return ExitDecision("SPOT_HI", pnl, peak)
        if be is not None and e is not None and ((be > e and spot >= be) or (be < e and spot <= be)):
            if be > e:
                st["spot_lo"] = max(lo if lo is not None else -math.inf, e)
            else:
                st["spot_hi"] = min(hi if hi is not None else math.inf, e)
            st["spot_be"] = None
            self._info("EXIT-ENGINE breakeven %s: stop moved to entry %.1f", plan.variant_id, e)
        return ExitDecision(None, pnl, peak)

    def _candidate_floor(self, peak: float, giveback: float, arm: Optional[float] = None) -> float:
        p = self.p
        arm = p.arm_rs if arm is None else arm
        pct = peak * (1.0 - giveback)
        ladder = -math.inf
        if p.ladder_step_rs > 0:
            k = max(math.floor((peak - arm) / p.ladder_step_rs), 0)        # highest milestone reached: arm + k x step
            ladder = arm + (k - p.ladder_lag_steps) * p.ladder_step_rs
        return max(pct, ladder, arm)

    # ------------------------------------------------------------------ main
    def evaluate(self, pos, pnl: float, now: dt.datetime, sig: Optional[ContextSignals] = None,
                 spot: Optional[float] = None) -> ExitDecision:
        """One tick / bar. Never raises; on an internal error it still enforces the hard stop."""
        p = self.p
        try:
            st = pos.exit_state
            if not st:
                self.init_state(pos, None, spot or (sig.spot if sig else 0.0))
                st = pos.exit_state
            if spot:
                st["hi"], st["lo"] = max(st.get("hi") or spot, spot), min(st.get("lo") or spot, spot)
            pos.peak_pnl = max(pos.peak_pnl, pnl)
            peak = pos.peak_pnl
            stop, arm = self._levels(pos, st)
            if stop > 0 and pnl <= -stop:
                self._warn("EXIT-ENGINE HARD_STOP %s pnl=%.0f <= -%.0f", pos.plan.variant_id, pnl, stop)
                return ExitDecision("HARD_STOP", pnl, peak, st.get("floor"), bool(st.get("armed")))
            if getattr(pos.plan, "spot_rules", False):
                return self._spot_exit(pos, st, pnl, peak, spot)
            if peak < arm:
                return ExitDecision(None, pnl, peak, None, False, p.base_giveback)      # neutral zone: hold
            if not st.get("armed"):
                st["armed"] = True
                self._info("EXIT-ENGINE trail ARMED %s peak=%.0f pnl=%.0f", pos.plan.variant_id, peak, pnl)
            gb, notes = self._adapt(st, pos, sig, spot, now)
            cand = self._candidate_floor(peak, gb, arm)
            prev = st.get("floor")
            floor = cand if prev is None else max(prev, cand)                          # ratchet: never lowered
            if prev is None or floor > prev + 1e-9:
                self._info("EXIT-ENGINE floor %s -> %.0f (peak %.0f, giveback %.0f%%, pnl %.0f)",
                         pos.plan.variant_id, floor, peak, gb * 100, pnl)
            if notes != st.get("notes"):
                self._info("EXIT-ENGINE adaptation %s: %s", pos.plan.variant_id, "; ".join(notes) or "none")
                st["notes"] = notes
            st["floor"] = floor
            if pnl < floor:
                self._warn("EXIT-ENGINE TRAILING_STOP %s pnl=%.0f < floor %.0f (peak %.0f)",
                            pos.plan.variant_id, pnl, floor, peak)
                return ExitDecision("TRAILING_STOP", pnl, peak, floor, True, gb, tuple(notes))
            return ExitDecision(None, pnl, peak, floor, True, gb, tuple(notes))
        except Exception:  # noqa: BLE001 - a bug here must not disable the stop
            log.exception("ExitEngine.evaluate failed; applying hard stop only")
            if p.hard_stop_rs > 0 and pnl <= -p.hard_stop_rs:
                return ExitDecision("HARD_STOP", pnl, pos.peak_pnl)
            return ExitDecision(None, pnl, pos.peak_pnl)
