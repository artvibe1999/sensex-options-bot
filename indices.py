"""The six index-option markets SENSEX PRO scans: where each trades and its strike interval.

Lot sizes, tick sizes, strikes and expiries are NOT here: they are read from Kite's instrument master every session
(they change). `default_lots` is the user's per-index lot setting before any Telegram /lots change (the order is
still reduced to whatever fits the premium cap, down to one lot, or skipped).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexSpec:
    name: str                     # option "name" in the instruments dump
    exchange: str                 # options (and futures) exchange: NFO | BFO
    spot_exchange: str            # NSE | BSE
    spot_symbol: str              # the index's tradingsymbol in the NSE / BSE dump
    step: float                   # strike interval used for ATM rounding (checked against the dump at runtime)
    default_lots: int

    @property
    def key(self) -> str:
        return f"{self.spot_exchange}:{self.spot_symbol}"


INDICES: dict[str, IndexSpec] = {
    "SENSEX": IndexSpec("SENSEX", "BFO", "BSE", "SENSEX", 100.0, 2),
    "NIFTY": IndexSpec("NIFTY", "NFO", "NSE", "NIFTY 50", 50.0, 1),
    "BANKNIFTY": IndexSpec("BANKNIFTY", "NFO", "NSE", "NIFTY BANK", 100.0, 2),
    "FINNIFTY": IndexSpec("FINNIFTY", "NFO", "NSE", "NIFTY FIN SERVICE", 50.0, 1),
    "MIDCPNIFTY": IndexSpec("MIDCPNIFTY", "NFO", "NSE", "NIFTY MID SELECT", 25.0, 1),
    "NIFTYNXT50": IndexSpec("NIFTYNXT50", "NFO", "NSE", "NIFTY NEXT 50", 100.0, 2),
}
