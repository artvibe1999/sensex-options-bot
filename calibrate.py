"""Measure the backtest's key assumption from YOUR recorded data.

The backtest prices options with ATM IV = India VIX x iv_scale (default 0.95). The bot records the real
option chain, so this compares the actual ATM implied vol (average of the nearest-ATM call and put) with
India VIX and suggests an iv_scale.

  docker compose exec bot python calibrate.py [--since 2026-10-22]

If the suggested value is lower than the one in use, the backtest has been OVERSTATING the premium that
option sellers collect: lower BT_IV_SCALE in .env, re-run backtest.py and re-check the approvals.
Needs a few full trading days of data to mean anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import statistics

from backtest import _years
from config import load_config
from strategy_engine import implied_vol


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="0000", help="only rows with ts >= this (e.g. 2026-10-22)")
    ap.add_argument("--every", type=int, default=6, help="use every Nth snapshot (default 6 = about 1/min)")
    args = ap.parse_args()
    cfg = load_config()
    con = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, spot, vix FROM market WHERE ts>=? AND vix>0 ORDER BY ts", (args.since,)).fetchall()
    ratios: list[float] = []
    for i, (ts, spot, vix) in enumerate(rows):
        if i % args.every:
            continue
        ch = con.execute("SELECT expiry, strike, kind, ltp FROM chain WHERE ts=? AND ltp>0", (ts,)).fetchall()
        if not ch:
            continue
        when = dt.datetime.fromisoformat(ts)
        atm = min({c[1] for c in ch}, key=lambda k: abs(k - spot))
        ivs = []
        for expiry, strike, kind, ltp in ch:
            if strike != atm:
                continue
            exp = dt.date.fromisoformat(expiry)
            if (exp - when.date()).days < 1:
                continue                                   # the backtest never trades 0-DTE
            # SAME clock as the backtest's pricing (trading time, 252 days): the bot's stored IV uses calendar time,
            # which differs by a DTE-dependent factor (weekends, overnight) and would bias the suggestion.
            iv = implied_vol(ltp, spot, strike, _years(exp, when), cfg.strategy.risk_free_rate, kind)
            if iv:
                ivs.append(iv)
        if len(ivs) < 2 or not 0.02 < statistics.mean(ivs) < 2.0:
            continue
        ratios.append(statistics.mean(ivs) * 100.0 / vix)
    used = cfg.backtest.iv_scale
    print(f"snapshots used: {len(ratios)} of {len(rows)} (since {args.since})")
    if len(ratios) < 100:
        print("Not enough data yet: collect at least a couple of full trading days, then run again.")
        return 1
    ratios.sort()
    q = lambda p: ratios[int(p * (len(ratios) - 1))]
    med = statistics.median(ratios)
    print(f"ATM IV / India VIX  median {med:.3f}   10th pct {q(0.1):.3f}   90th pct {q(0.9):.3f}")
    print(f"iv_scale now in use : {used:.3f}")
    print(f"suggested BT_IV_SCALE: {med:.2f}")
    if med < used - 0.03:
        print("-> Real SENSEX option IV is LOWER than the backtest assumes: sellers' backtest profits are "
              "overstated. Set BT_IV_SCALE in .env, re-run backtest.py, restart the bot.")
    elif med > used + 0.03:
        print("-> Real IV is higher than assumed: the backtest is conservative for sellers.")
    else:
        print("-> The assumption is close to reality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
