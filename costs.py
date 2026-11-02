"""Transaction-cost model shared by the backtester, shadow book and live PnL.

Rates verified against Zerodha's own charges (Kite virtual contract note): 54 real BSE SENSEX option orders on
2 Nov 2026, and a sweep of order sizes on NFO and BFO on 3 Nov 2026. Brokerage Rs 20 per order; STT 0.15% of premium
on sells (from 1 April 2026); exchange transaction charge 0.0325% (BSE) / 0.03553% (NSE) of premium; SEBI Rs 10 per
crore; GST 18% on brokerage + exchange + SEBI; stamp duty 0.003% on buys, rounded to the NEAREST rupee per order
(Rs 0.49 -> 0, Rs 0.60 -> 1, Rs 1.50 -> 2).
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Iterable

NSE_EXCH_TXN_PCT = 0.0003553          # NFO index options (Zerodha contract note, 3 Nov 2026)


@dataclass(frozen=True)
class CostParams:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.0015       # on premium, sell side (from 1 Apr 2026)
    exch_txn_pct: float = 0.000325     # BSE index options, on premium turnover (matches Zerodha)
    sebi_pct: float = 0.000001         # Rs 10 per crore
    stamp_buy_pct: float = 0.00003     # buy side, rounded to the nearest rupee per order
    gst_pct: float = 0.18              # on brokerage + exchange + SEBI charges

    def for_exchange(self, exchange: str) -> "CostParams":
        """The same schedule with the exchange's transaction charge (NFO: NSE rate; BFO and anything else: as set)."""
        if exchange == "NFO":
            return dataclasses.replace(self, exch_txn_pct=NSE_EXCH_TXN_PCT)
        return self


def _stamp(turnover: float, pct: float) -> float:
    return float(math.floor(turnover * pct + 0.5)) if turnover > 0 else 0.0


def trade_cost(fills: Iterable[tuple[str, float, int]], p: CostParams) -> float:
    """Total charges for fills given as (side, price, quantity), each fill one order."""
    total = 0.0
    for side, price, qty in fills:
        turnover = price * qty
        brokerage = p.brokerage_per_order
        exch = turnover * p.exch_txn_pct
        sebi = turnover * p.sebi_pct
        gst = (brokerage + exch + sebi) * p.gst_pct
        stt = turnover * p.stt_sell_pct if side == "SELL" else 0.0
        stamp = _stamp(turnover, p.stamp_buy_pct) if side == "BUY" else 0.0
        total += brokerage + exch + sebi + gst + stt + stamp
    return total
