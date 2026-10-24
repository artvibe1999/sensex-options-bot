"""How much of the strategy library can an account of a given size actually trade?

    python capital_check.py [--spot 73000] [--capitals 15000,50000,150000,500000]

Builds a model option chain (Black-Scholes, the backtest's own pricing) for each variant's regime and
expiry band, then asks the real RiskManager whether 1+ lots fit the account's risk limits.
It shows worst-case loss per lot, round-trip costs, and the profile used. It does NOT predict profit.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt

import backtest as bt
import strategy_library as lib
from config import IST, BacktestParams, RiskParams, Schedule, StrategyParams, risk_for_capital
from costs import CostParams, trade_cost
from risk_manager import RiskManager
from strategy_engine import Regime, RegimeState

IV = {"LOW_VOL": 0.11, "NORMAL": 0.15, "HIGH_VOL": 0.22}


def analyse(spot: float, capitals: list[float]) -> dict:
    bp, sp, cp = BacktestParams(), StrategyParams(), CostParams()
    now = dt.datetime(2026, 10, 21, 10, 0, tzinfo=IST)
    today = now.date()
    plans = []
    for spec in lib.build_library():
        if spec.oi_rule != "none":
            continue                                           # same trades as their plain twins
        dte = 2 if spec.dte_max <= 3 else 5
        regime = spec.regimes[0]
        chain = bt.model_chain(spot, IV[regime], today + dt.timedelta(days=dte), now, bp, sp.risk_free_rate)
        rs = RegimeState(Regime(regime), 15.0, 1.0, 12.0, False, 0)
        plan = lib.build_plan(spec, chain, rs, lib.TEMPLATE_DIRECTION.get(spec.template, 0), today, now)
        if plan:
            fills = [(l.side, l.ref_price, plan.qty_for(l)) for l in plan.legs]
            fills += [("SELL" if l.side == "BUY" else "BUY", l.ref_price, plan.qty_for(l)) for l in plan.legs]
            plans.append((spec, plan, trade_cost(fills, cp)))
    out = {"n": len(plans), "caps": {}}
    for cap in capitals:
        rp = risk_for_capital(RiskParams(), cap)
        rm = RiskManager(rp, Schedule())
        ok = [(s, p, c) for s, p, c in plans if rm.size(p, cap) >= 1]
        by_t: dict = collections.defaultdict(list)
        for s, p, c in ok:
            by_t[s.template].append((p.max_loss_per_lot, c, p.max_profit_per_lot))
        out["caps"][cap] = {"profile": "small" if rp.one_lot_risk_pct else "standard", "tradable": len(ok),
                            "min_loss": min((p.max_loss_per_lot for _, p, _ in ok), default=None),
                            "templates": {t: (len(v), min(x[0] for x in v), sum(x[1] for x in v) / len(v))
                                          for t, v in by_t.items()}}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", type=float, default=73000.0)
    ap.add_argument("--capitals", default="15000,50000,150000,500000")
    a = ap.parse_args()
    caps = [float(x) for x in a.capitals.split(",")]
    res = analyse(a.spot, caps)
    print(f"Model chain at spot {a.spot:,.0f}; {res['n']} plain variants could form a plan.\n")
    for cap, d in res["caps"].items():
        print(f"=== Capital Rs {cap:,.0f}  (profile: {d['profile']}) ===")
        print(f"  tradable variants: {d['tradable']} of {res['n']}")
        if not d["tradable"]:
            print("  NONE: one lot of every strategy risks more than this account's limit.")
            continue
        print(f"  smallest worst-case loss per lot: Rs {d['min_loss']:,.0f} ({d['min_loss'] / cap:.0%} of capital)")
        for t, (n, ml, cost) in sorted(d["templates"].items()):
            print(f"    {t:<17} {n:>3} variants | least max-loss/lot Rs {ml:>7,.0f} | avg round-trip cost Rs {cost:,.0f}")
        print()
    print("Costs are a fixed Rs per order, so they weigh most on small lots. This tool shows what FITS, not what profits.")


if __name__ == "__main__":
    main()
