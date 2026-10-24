"""Mocked live-market deployment harness (no network, no real orders).

  python sim_harness.py            # runs every strategy through a simulated session and prints a report

Two parts:
  * MockKite: a Kite-like client with scripted fill behaviour (fill / partial / reject / hang / raise) that records
    every call in order, so tests can assert the BUY-first / verified-fill / SELL-second sequence.
  * simulate_session: mocked tick data (random-walk spot, Black-Scholes option marks with time decay) driving the
    real RiskManager (stop-loss, take-profit, trailing stop, square-off), PaperBroker and the strategy classes.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

import backtest as bt
from config import IST, BacktestParams, ExecutionParams, RiskParams, Schedule, StrategyParams
from execution import OrderLeg, PaperBroker, reverse_legs
from option_strategies import STRATEGY_CLASSES, EntryContext, OptionStrategy
from risk_manager import ManagedPosition, OpenLeg, RiskManager
from strategy_engine import Leg, TradePlan, bs_price


# ------------------------------------------------------------------- mock broker API
@dataclass
class MockKite:
    """behaviour: symbol -> 'fill' | 'partial' | 'reject' | 'hang' | 'raise' (default 'fill')."""
    behaviour: dict = field(default_factory=dict)
    cash: float = 1_000_000.0
    margin_per_order: float = 10_000.0
    margin_fails: bool = False
    log: list = field(default_factory=list)          # ("place"|"cancel"|"complete", side, symbol, qty, market)
    _orders: dict = field(default_factory=dict)
    _n: int = 0

    # --- order API (same shape as ZerodhaClient)
    def place_order(self, c, side, qty, product, price, tag, market=False):
        mode = self.behaviour.get((c.symbol, side), self.behaviour.get(c.symbol, "fill"))
        if mode == "raise":
            self.log.append(("place_error", side, c.symbol, qty, market))
            raise TimeoutError("simulated network timeout")
        self._n += 1
        oid = f"OID{self._n}"
        self._orders[oid] = {"c": c, "side": side, "qty": qty, "price": price, "mode": mode, "cancelled": False,
                             "tag": tag}
        if mode == "raise_placed":              # order reached the exchange but the reply was lost
            self.log.append(("place", side, c.symbol, qty, market))
            raise TimeoutError("simulated lost reply")
        self.log.append(("place", side, c.symbol, qty, market))
        return oid

    def order_by_tag(self, tag):
        for oid, o in self._orders.items():
            if o.get("tag") == tag:
                return oid
        return None

    def order_history(self, oid):
        o = self._orders[oid]
        if o["cancelled"]:
            return [{"status": "CANCELLED", "filled_quantity": o.get("filled", 0), "average_price": o["price"]}]
        if o["mode"] == "reject":
            return [{"status": "REJECTED", "filled_quantity": 0, "average_price": 0,
                     "status_message": "Insufficient funds (simulated margin rejection)"}]
        if o["mode"] == "hang":
            return [{"status": "OPEN", "filled_quantity": 0, "average_price": 0}]
        if o["mode"] == "partial":
            o["filled"] = o["qty"] // 2
            return [{"status": "OPEN", "filled_quantity": o["filled"], "average_price": o["price"]}]
        if not o.get("done"):
            o["done"] = True
            self.log.append(("complete", o["side"], o["c"].symbol, o["qty"], False))
        return [{"status": "COMPLETE", "filled_quantity": o["qty"], "average_price": o["price"]}]

    def cancel_order(self, oid):
        self._orders[oid]["cancelled"] = True
        self.log.append(("cancel", self._orders[oid]["side"], self._orders[oid]["c"].symbol, 0, False))

    # --- margin / cash
    def required_margin(self, legs, product):
        return None if self.margin_fails else self.margin_per_order * len(list(legs))

    def available_cash(self):
        return self.cash


def fast_cfg() -> SimpleNamespace:
    return SimpleNamespace(execution=ExecutionParams(fill_timeout_s=0.05))


# --------------------------------------------------------------- plan construction
def sample_chain(spot: float = 80000.0, dte: int = 2, iv: float = 0.15):
    bp, sp = BacktestParams(), StrategyParams()
    now = dt.datetime(2026, 10, 26, 9, 30, tzinfo=IST)
    return bt.model_chain(spot, iv, now.date() + dt.timedelta(days=dte), now, bp, sp.risk_free_rate), now, bp, sp


def _unique_tokens(legs: tuple[Leg, ...]) -> tuple[Leg, ...]:
    return tuple(dataclasses.replace(l, contract=dataclasses.replace(l.contract, token=1000 + i))
                 for i, l in enumerate(legs))


def plan_from_strategy(strat: OptionStrategy, chain) -> Optional[TradePlan]:
    built = strat.build(chain)
    if not built:
        return None
    legs, eco = built
    legs = _unique_tokens(legs)
    r = strat.exit_rules
    return TradePlan(strat.label.upper(), "NORMAL", chain.expiry, legs, legs[0].contract.lot_size,
                     eco.max_loss, eco.max_profit, strat.describe(eco), f"os_{strat.key}",
                     sl_frac=r.sl_frac, tp_frac=r.tp_frac)


def order_legs(plan: TradePlan, lots: int = 1) -> list[OrderLeg]:
    return [OrderLeg(l.contract, l.side, plan.qty_for(l, lots), l.ref_price) for l in plan.legs]


# --------------------------------------------------------------------- tick session
@dataclass
class SessionResult:
    strategy: str
    reason: str
    exit_time: dt.time
    pnl: float
    max_loss: float
    max_profit: float
    ticks: int


def simulate_session(plan: TradePlan, chain, now: dt.datetime, bp: BacktestParams, sp: StrategyParams,
                     seed: int = 1, drift: float = 0.0, vol: float = 0.15, lots: int = 1) -> SessionResult:
    """One session of mocked 1-minute ticks. The real RiskManager decides the exit."""
    rnd = random.Random(seed)
    sched, risk = Schedule(), RiskManager(RiskParams(), Schedule())
    broker = PaperBroker(0.005)
    fills = broker.execute(order_legs(plan, lots), "MIS")
    legs = [OpenLeg(l.contract, l.side, plan.qty_for(l, lots), fills[l.contract.symbol]) for l in plan.legs]
    pos = ManagedPosition(plan, lots, legs, now, plan.max_loss_per_lot * lots, plan.max_profit_per_lot * lots)
    spot, t, ticks = chain.spot, now, 0
    sigma_min = vol / (252 * 375) ** 0.5
    while True:
        t += dt.timedelta(minutes=1)
        ticks += 1
        spot *= 1 + drift / (252 * 375) + rnd.gauss(0, sigma_min)
        ltps = {}
        for l in plan.legs:
            T = max(bt._years(plan.expiry, t), 1e-6)
            sig = bt.skew_iv(0.15, spot, l.contract.strike, bp.skew_slope)
            ltps[l.contract.token] = max(bs_price(spot, l.contract.strike, T, sp.risk_free_rate, sig,
                                                  l.contract.kind), 0.05)
        pnl = pos.pnl(ltps)
        reason = risk.check_position(pos, pnl)
        if reason is None and t.time() >= sched.square_off:
            reason = "SQUARE_OFF_TIME"
        if reason:
            closing = reverse_legs([OrderLeg(l.contract, l.side, l.qty, ltps[l.contract.token]) for l in legs])
            out = broker.execute(closing, "MIS")
            assert out, "paper exit must always fill"
            return SessionResult(plan.variant_id, reason, t.time(), round(pnl, 2), pos.max_loss, pos.max_profit, ticks)


def main() -> int:
    chain, now, bp, sp = sample_chain()
    print(f"{'strategy':<26}{'exit':<18}{'time':<7}{'P&L':>9}{'max loss':>10}{'max profit':>11}")
    for key, cls in STRATEGY_CLASSES.items():
        strat = cls()
        plan = plan_from_strategy(strat, chain)
        if not plan:
            print(f"{key:<26}(no plan on this chain)")
            continue
        for seed, drift in ((1, 0.0), (2, 8.0), (3, -8.0)):
            r = simulate_session(plan, chain, now, bp, sp, seed=seed, drift=drift)
            print(f"{key:<26}{r.reason:<18}{r.exit_time:%H:%M}  {r.pnl:>9,.0f}{r.max_loss:>10,.0f}{r.max_profit:>11,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
