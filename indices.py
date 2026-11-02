"""The six index-option markets the multi-index engine scans, with everything that differs between them.

The only place index constants live. Lot sizes and expiries are read from Kite's instrument dump at runtime (they
change); the values here are what the dump said on 3 Nov 2026 and are used only for checks and tests.

`setups` / `params` / `edge` come from the six-index backtest (lab6, 2026-11-03: bot playbook code replayed on Kite
5-minute history 2018-2026, real Kite margins, Zerodha charges): a setup is enabled for an index only if it was
profitable on 2018-22, profitable on 2023-26 and not negative at double slippage. `edge` is the 2023-26 average net
per trade in rupees (per the index's default lots), used to rank simultaneous signals.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Mapping

SETUPS = ("single", "condor", "credit")


@dataclass(frozen=True)
class IndexSpec:
    name: str                     # option "name" in the instruments dump
    exchange: str                 # options exchange: NFO | BFO
    spot_exchange: str            # NSE | BSE
    spot_symbol: str              # the index's tradingsymbol in the NSE / BSE dump
    step: float                   # strike interval
    default_lots: int             # the user's default lots (Telegram /lots <INDEX> <N> changes it)
    monthly_only: bool            # no weekly contracts: trade only in the expiry week (<= expiry_week_days left)
    max_spread_pct: float         # every leg needs a two-sided quote at most this wide (% of mid)
    setups: frozenset = frozenset()                      # enabled playbook setups
    params: Mapping[str, float] = field(default_factory=dict)   # PlaybookParams overrides
    edge: Mapping[str, float] = field(default_factory=dict)     # setup -> 2023-26 average net per trade (Rs)
    itm_by_steps: bool = False    # SENSEX: the deployed 2 / 4 strike-step ITM rule; others: by % of spot

    @property
    def key(self) -> str:
        return f"{self.spot_exchange}:{self.spot_symbol}"


EXPIRY_WEEK_DAYS = 4
ITM_PCT = (0.0027, 0.0053)        # SENSEX's 2 / 4 steps at SENSEX 75,000, as a share of spot

_BASE = {
    "SENSEX": IndexSpec("SENSEX", "BFO", "BSE", "SENSEX", 100.0, 2, False, 3.0, itm_by_steps=True),
    "NIFTY": IndexSpec("NIFTY", "NFO", "NSE", "NIFTY 50", 50.0, 1, False, 3.0),
    "BANKNIFTY": IndexSpec("BANKNIFTY", "NFO", "NSE", "NIFTY BANK", 100.0, 2, True, 3.0),
    "FINNIFTY": IndexSpec("FINNIFTY", "NFO", "NSE", "NIFTY FIN SERVICE", 50.0, 1, True, 3.0),
    "MIDCPNIFTY": IndexSpec("MIDCPNIFTY", "NFO", "NSE", "NIFTY MID SELECT", 25.0, 1, True, 3.0),
    "NIFTYNXT50": IndexSpec("NIFTYNXT50", "NFO", "NSE", "NIFTY NEXT 50", 100.0, 2, True, 3.0),
}

# From the six-index backtest (lab6, 2026-11-03). Edge = 2018-22 average net per trade (no look-ahead into 2023-26).
# FINNIFTY and NIFTYNXT50: no setup was profitable in both periods (NIFTYNXT50 lost in every variant and its options
# barely trade: 33 lots near the money on 2 Nov 2026), so they are scanned but take no trades.
_WIDE_CONDOR = {"condor_short_pct_high_vix": 0.012, "condor_short_pct_low_vix": 0.010, "condor_wing_pct": 0.025}
_SELECTED: dict[str, dict] = {
    "SENSEX": {"setups": ("single", "condor", "credit"), "edge": {"single": 2162.6, "condor": 551.8, "credit": 68.4}},
    "NIFTY": {"setups": ("single", "condor"), "params": _WIDE_CONDOR, "edge": {"single": 1166.9, "condor": 266.4}},
    "BANKNIFTY": {"setups": ("credit",), "edge": {"credit": 71.1}},
    "MIDCPNIFTY": {"setups": ("condor",), "params": _WIDE_CONDOR, "edge": {"condor": 630.5}},
    "FINNIFTY": {},
    "NIFTYNXT50": {},
}


def _build() -> dict[str, IndexSpec]:
    out = {}
    for name, spec in _BASE.items():
        sel = _SELECTED.get(name, {})
        out[name] = dataclasses.replace(spec, setups=frozenset(sel.get("setups", ())),
                                        params=dict(sel.get("params", {})), edge=dict(sel.get("edge", {})))
    return out


INDICES: dict[str, IndexSpec] = _build()
