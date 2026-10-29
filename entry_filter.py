"""Entry signal evaluator: confirm direction, IV skew and institutional OI build-up before a multi-leg order is sent.

`evaluate_entry(plan, snap, sig, params, ...)` returns an `EntryDecision` listing every check, whether it passed and
why, so a rejection is always explainable in the logs. All checks must pass (AND). A missing input (no skew, no OI
history yet) FAILS the check when `strict` (the default): an unconfirmed entry is not taken.

Checks
  reach       the structure's maximum profit can actually cross the trailing-stop arm level (otherwise the exit
              engine could never lock a profit on it)
  cost        the arm level is at least `min_arm_cost_mult` x round-trip charges
  theta       net premium sellers forced to square off the same day: expected intraday theta covers the charges
  direction   directional structures need the daily and/or intraday trend (or a trap reversal) to agree and none to
              oppose; delta-neutral structures need NO valid trend, judged on a formed intraday read
  skew        25-delta IV skew (put IV - call IV, vol points) must not contradict the structure
  oi          price-vs-OI build-up (long / short build-up, short covering, long unwinding) and option-writing side
              must agree with the structure; neutral structures need a quiet, non-trending OI picture
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from exit_engine import describe_position
from market_context import BUILDUP_BIAS, ContextSignals

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        log.warning("Bad %s value; using %s", name, default)
        return default


@dataclass(frozen=True)
class EntryParams:
    enabled: bool = True
    strict: bool = True                   # missing data fails the check
    skew_bull_max: float = 8.0            # bullish structures: reject if puts are bid this hard over calls
    skew_bear_min: float = -1.0           # bearish structures: reject if calls are bid over puts (negative skew)
    skew_neutral_abs: float = 8.0         # neutral structures: reject if the skew is stressed either way
    neutral_vix_fall_pct: float = 3.0     # long-volatility structures: reject when VIX is falling this fast
    min_arm_cost_mult: float = 2.0        # the trail's arm level must be at least this multiple of round-trip costs
    # Premium-selling structures squared off the same day (MIS) only earn the theta of the hours left. Expected
    # capture = net theta/day x theta_capture_frac x share of the session left; it must cover costs x the multiple.
    theta_capture_frac: float = 0.5
    min_theta_cost_mult: float = 1.5

    @classmethod
    def from_env(cls) -> "EntryParams":
        d = cls()
        return cls(enabled=(os.getenv("ENTRY_FILTER", "1") or "1") != "0",
                   strict=(os.getenv("ENTRY_FILTER_STRICT", "1") or "1") != "0",
                   skew_bull_max=_env_float("SKEW_BULL_MAX", d.skew_bull_max),
                   skew_bear_min=_env_float("SKEW_BEAR_MIN", d.skew_bear_min),
                   skew_neutral_abs=_env_float("SKEW_NEUTRAL_ABS", d.skew_neutral_abs),
                   min_arm_cost_mult=_env_float("MIN_ARM_COST_MULT", d.min_arm_cost_mult),
                   theta_capture_frac=_env_float("THETA_CAPTURE_FRAC", d.theta_capture_frac),
                   min_theta_cost_mult=_env_float("MIN_THETA_COST_MULT", d.min_theta_cost_mult))


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EntryDecision:
    ok: bool
    bias: int
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return "; ".join(f"{c.name}: {c.detail}" for c in self.checks if not c.passed) or "all checks passed"

    def summary(self) -> str:
        return " | ".join(f"{c.name}={'OK' if c.passed else 'NO'} ({c.detail})" for c in self.checks)


def plan_bias(plan, chain) -> dict:
    """Net delta bias and vega of a TradePlan (one lot), using the live chain greeks."""
    legs = [SimpleNamespace(contract=l.contract, side=l.side, qty=plan.qty_for(l, 1)) for l in plan.legs]
    return describe_position(legs, chain, plan.name)


def _missing(strict: bool, what: str) -> Check:
    return Check(what, not strict, "no data" + ("" if not strict else " (strict: fail closed)"))


def evaluate_entry(plan, snap, sig: Optional[ContextSignals], params: EntryParams, lots: int = 1,
                   arm_rs: Optional[float] = None, cost_rs: Optional[float] = None,
                   session_left: Optional[float] = None) -> EntryDecision:
    """Never raises: any internal error is a rejection (fail closed).

    cost_rs: round-trip charges for all lots. session_left: share (0-1) of the trading session left before a forced
    same-day square-off (None = the position may be held overnight, so the intraday theta check does not apply)."""
    try:
        if not params.enabled:
            return EntryDecision(True, 0, (Check("filter", True, "disabled"),))
        desc = plan_bias(plan, snap.chain)
        bias, vega = desc["bias"], desc["vega"]
        checks: list[Check] = []

        # --- reachability of the trailing arm level, and an arm level worth reaching after costs
        if arm_rs is not None:
            mp = plan.max_profit_per_lot * lots
            checks.append(Check("reach", mp > arm_rs, f"max profit {mp:,.0f} vs arm {arm_rs:,.0f}"))
            if cost_rs is not None:
                need = params.min_arm_cost_mult * cost_rs
                checks.append(Check("cost", arm_rs >= need, f"arm {arm_rs:,.0f} vs {params.min_arm_cost_mult:g}x "
                                                            f"costs {cost_rs:,.0f}"))

        # --- same-day premium selling must be able to earn its costs from theta
        if session_left is not None and cost_rs is not None and desc["greeks"] and desc["theta"] > 0:
            capture = desc["theta"] * lots * params.theta_capture_frac * max(session_left, 0.0)
            need = params.min_theta_cost_mult * cost_rs
            checks.append(Check("theta", capture >= need, f"intraday theta ~{capture:,.0f} vs "
                                                          f"{params.min_theta_cost_mult:g}x costs {cost_rs:,.0f}"))

        # --- direction
        td = snap.trend_daily
        ti = snap.tinfo
        intr = getattr(ti, "intraday", None) if ti is not None else None
        daily = getattr(ti, "daily", None) if ti is not None else None
        votes = [v for v in (td, getattr(intr, "dir", None)) if v]
        traps = [t for t in (getattr(daily, "trap_dir", 0), getattr(intr, "trap_dir", 0)) if t]
        if bias != 0:
            against = [v for v in votes + traps if v == -bias]
            with_ = [v for v in votes + traps if v == bias]
            if against:
                checks.append(Check("direction", False, f"trend/trap opposes {'bull' if bias > 0 else 'bear'} structure"))
            elif with_:
                checks.append(Check("direction", True, f"{len(with_)} signal(s) confirm"))
            else:
                checks.append(_missing(params.strict, "direction") if not votes and not traps
                              else Check("direction", False, "no confirming trend"))
        else:
            valid = bool(getattr(daily, "valid", False)) or bool(getattr(intr, "valid", False))
            if intr is None or not getattr(intr, "ready", True):
                # "no trend" before the intraday read has formed is unknown, not a range
                checks.append(_missing(params.strict, "direction"))
            else:
                checks.append(Check("direction", not valid, "valid trend present: neutral structure unsafe" if valid
                                    else "no valid trend"))
            if vega > 0 and sig is not None and sig.vix_chg_pct is not None:
                falling = sig.vix_chg_pct <= -params.neutral_vix_fall_pct
                checks.append(Check("vix", not falling, f"long-vega with VIX {sig.vix_chg_pct:+.1f}%"))

        # --- IV skew (25d put IV - 25d call IV, vol points)
        skew = getattr(snap.features, "skew", None) if snap.features is not None else None
        if skew is None:
            checks.append(_missing(params.strict, "skew"))
        elif bias > 0:
            checks.append(Check("skew", skew <= params.skew_bull_max, f"{skew:+.1f} vs max {params.skew_bull_max:+.1f}"))
        elif bias < 0:
            checks.append(Check("skew", skew >= params.skew_bear_min, f"{skew:+.1f} vs min {params.skew_bear_min:+.1f}"))
        else:
            checks.append(Check("skew", abs(skew) <= params.skew_neutral_abs,
                                f"{skew:+.1f} vs +/-{params.skew_neutral_abs:.1f}"))

        # --- institutional OI build-up
        if sig is None or sig.spot_chg_pct is None or sig.call_oi_chg is None:
            checks.append(_missing(params.strict, "oi"))
        else:
            bb, wb = BUILDUP_BIAS.get(sig.buildup, 0), sig.write_bias
            label = f"{sig.buildup}, writing {wb:+d}"
            if bias != 0:
                if bb == -bias or wb == -bias:
                    checks.append(Check("oi", False, f"{label} opposes structure"))
                elif bb == bias or wb == bias:
                    checks.append(Check("oi", True, f"{label} confirms"))
                else:
                    checks.append(Check("oi", False, f"{label}: no institutional confirmation"))
            else:
                checks.append(Check("oi", bb == 0, f"{label}: " + ("quiet" if bb == 0 else "directional build-up")))

        return EntryDecision(all(c.passed for c in checks), bias, tuple(checks))
    except Exception:  # noqa: BLE001
        log.exception("evaluate_entry failed; rejecting entry")
        return EntryDecision(False, 0, (Check("error", False, "internal error (see log)"),))
