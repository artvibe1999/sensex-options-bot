"""Option contract selection (brief section 5) and the whole-lot sizing rule (1.2).

Kite does not supply Greeks, and the brief forbids fabricating them, so delta is reported as unavailable and the
selector falls back to ATM proximity: ATM, one strike in the money, one out of the money, never farther. Buy cost is
always the ASK (never LTP); one whole lot must fit the premium cap including entry charges, else no trade.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Iterable, Mapping, Optional, Sequence

from costs import CostParams, trade_cost

from .models import CE, ContractMeta, OptionQuote
from .settings import RiskLimits
from .strategies.scorer import option_liquidity_points

DELTA_FIT = {0: 5.0, -1: 4.0, 1: 2.0}                   # ATM, 1 ITM, 1 OTM (no Greeks: proximity only)


@dataclass(frozen=True)
class Selection:
    contract: ContractMeta
    quote: OptionQuote
    lots: int
    qty: int
    ask: float
    bid: float
    entry_charges: float
    exit_charges_est: float
    outlay: float                       # ask x qty + entry charges
    spread_pct: float
    liquidity_pts: float
    moneyness_steps: int
    delta: Optional[float] = None       # unavailable: Kite supplies no Greeks
    alternatives: tuple = ()
    note: str = ""


@dataclass
class SelectionResult:
    selection: Optional[Selection]
    rejections: list = field(default_factory=list)      # (symbol or strike, reason)


def choose_expiry(contracts: Iterable[ContractMeta], today: dt.date, avoid_expiry_day: bool) -> Optional[dt.date]:
    exps = sorted({c.expiry for c in contracts if c.expiry >= today})
    if not exps:
        return None
    if avoid_expiry_day and exps[0] == today:
        return exps[1] if len(exps) > 1 else None
    return exps[0]


def strike_for(direction: str, atm: float, step: float, k: int) -> float:
    """k = 0 ATM, -1 one strike in the money, +1 one strike out of the money."""
    return atm + k * step if direction == CE else atm - k * step


def select_contract(direction: str, spot: float, step: float, contracts: Sequence[ContractMeta],
                    quotes: Mapping[int, OptionQuote], wanted_lots: int, max_lots: int, limits: RiskLimits,
                    costs: CostParams, p: SimpleNamespace, today: dt.date, now_mono: float, now: dt.datetime,
                    stale_s: float) -> SelectionResult:
    res = SelectionResult(None)
    expiry = choose_expiry(contracts, today, p.avoid_expiry_day_contract)
    if expiry is None:
        res.rejections.append(("-", "no eligible expiry in the instrument master"))
        return res
    by_key = {(c.strike, c.kind): c for c in contracts if c.expiry == expiry}
    atm = round(spot / step) * step
    cp = costs.for_exchange(next(iter(by_key.values())).exchange) if by_key else costs
    passing: list[tuple[float, Selection]] = []
    for k in p.candidate_steps:
        k = int(k)
        strike = strike_for(direction, atm, step, k)
        c = by_key.get((strike, direction))
        if c is None:
            res.rejections.append((f"{strike:g}{direction}", "not in the instrument master"))
            continue
        q = quotes.get(c.token)
        if q is None:
            res.rejections.append((c.symbol, "no quote"))
            continue
        why = q.problem(now_mono, stale_s, now)
        if why:
            res.rejections.append((c.symbol, why))
            continue
        sp = q.spread_pct
        if sp is None or sp > p.max_spread_pct:
            res.rejections.append((c.symbol, f"spread {sp:.2f}% > {p.max_spread_pct}%" if sp else "no spread"))
            continue
        if q.volume is None or q.volume < p.min_volume:
            res.rejections.append((c.symbol, f"volume {q.volume} < {p.min_volume}"))
            continue
        if c.lot_size <= 0:
            res.rejections.append((c.symbol, "invalid lot size"))
            continue
        chosen = None
        top = max(0, min(wanted_lots, max_lots))
        for n in range(top, 0, -1):
            qty = n * c.lot_size
            if c.freeze_qty and qty > c.freeze_qty:
                continue
            ent = trade_cost([("BUY", q.ask, qty)], cp)
            outlay = q.ask * qty + ent
            if outlay > limits.max_premium_per_entry:
                continue
            if q.ask_qty is None or q.ask_qty < min(n, int(p.min_depth_lots)) * c.lot_size:
                continue
            ext = trade_cost([("SELL", q.bid, qty)], cp)
            round_trip = (q.ask - q.bid) * qty + ent + ext
            if round_trip > limits.emergency_feasibility_frac * limits.emergency_loss_per_trade:
                continue
            chosen = Selection(c, q, n, qty, q.ask, q.bid, round(ent, 2), round(ext, 2), round(outlay, 2), round(sp, 3),
                               option_liquidity_points(sp), k)
            break
        if chosen is None:
            one = q.ask * c.lot_size + trade_cost([("BUY", q.ask, c.lot_size)], cp)
            res.rejections.append((c.symbol, f"one lot costs {one:,.0f} > cap {limits.max_premium_per_entry:,.0f}"
                                   if one > limits.max_premium_per_entry else
                                   "depth / freeze quantity / emergency feasibility not met"))
            continue
        rank = chosen.liquidity_pts + DELTA_FIT.get(k, 0.0) + (2.0 if chosen.lots == top else 0.0)
        passing.append((rank, chosen))
    if not passing:
        return res
    passing.sort(key=lambda x: -x[0])
    best = passing[0][1]
    alts = tuple({"symbol": s.contract.symbol, "lots": s.lots, "ask": s.ask, "spread_pct": s.spread_pct,
                  "rank": round(r, 1)} for r, s in passing[1:])
    note = (f"{best.contract.symbol}: {'ATM' if best.moneyness_steps == 0 else 'ITM1' if best.moneyness_steps < 0 else 'OTM1'}"
            f", delta unavailable (no broker Greeks), spread {best.spread_pct:.2f}%, {best.lots} lot(s)")
    res.selection = Selection(**{**best.__dict__, "alternatives": alts, "note": note})
    return res
