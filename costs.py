"""Transaction-cost model shared by the backtester, shadow book and live PnL.

Rates verified on 2 Nov 2026 against Zerodha's own charges (Kite virtual contract note) for 54 real BSE SENSEX
option orders: every component matched to the paisa once stamp duty is rounded UP to whole rupees per order
(Zerodha charges a Rs 1 minimum). STT on option premium sold is 0.15% from 1 April 2026 (Budget 2026).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CostParams:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.0015       # on premium, sell side (from 1 Apr 2026)
    exch_txn_pct: float = 0.000325     # BSE index options, on premium turnover (matches Zerodha)
    sebi_pct: float = 0.000001         # Rs 10 per crore
    stamp_buy_pct: float = 0.00003     # buy side, rounded up to whole rupees per order
    gst_pct: float = 0.18              # on brokerage + exchange + SEBI charges


def trade_cost(fills: Iterable[tuple[str, float, int]], p: CostParams) -> float:
    """Total charges for fills given as (side, price, quantity)."""
    total = 0.0
    for side, price, qty in fills:
        turnover = price * qty
        brokerage = p.brokerage_per_order
        exch = turnover * p.exch_txn_pct
        sebi = turnover * p.sebi_pct
        gst = (brokerage + exch + sebi) * p.gst_pct
        stt = turnover * p.stt_sell_pct if side == "SELL" else 0.0
        stamp = float(math.ceil(turnover * p.stamp_buy_pct)) if side == "BUY" and turnover > 0 else 0.0
        total += brokerage + exch + sebi + gst + stt + stamp
    return total
