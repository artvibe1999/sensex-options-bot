"""Promote variants to live: backtest approval AND forward (shadow) evidence.

  python promote.py                      # defaults: >=20 forward trades, PF >= 1.2, net > 0
  python promote.py --min-trades 30 --min-pf 1.3

Reads  data/approved_backtest.json  (from backtest.py)
       data/sxbot.db                (shadow_trades recorded by the bot every day)
Writes data/approved_live.json      (the ONLY file --live trades from; valid 30 days)

Re-run at least weekly. If you do not, the approvals expire and the live bot
stops opening new trades (fail closed).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3

from config import IST


def _pf(nets: list[float]) -> float:
    wins, losses = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    return 99.0 if losses == 0 and wins > 0 else (wins / losses if losses else 0.0)


def main() -> int:
    data_dir = os.getenv("DATA_DIR", "data")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=data_dir)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--min-pf", type=float, default=1.2)
    ap.add_argument("--lookback-days", type=int, default=90, help="forward window considered")
    ap.add_argument("--valid-days", type=int, default=30)
    args = ap.parse_args()

    with open(os.path.join(args.data_dir, "approved_backtest.json"), encoding="utf-8") as f:
        bt = json.load(f)
    since = (dt.date.today() - dt.timedelta(days=args.lookback_days)).isoformat()
    db = sqlite3.connect(os.path.join(args.data_dir, "sxbot.db"))
    fwd: dict[str, list[float]] = {}
    for vid, net in db.execute("SELECT variant, net FROM shadow_trades WHERE entry_ts >= ?", (since,)):
        fwd.setdefault(vid, []).append(net)
    db.close()

    live, rows = [], []
    for v in bt["variants"]:
        nets = fwd.get(v["id"], [])
        n, total, pf = len(nets), sum(nets), _pf(nets)
        ok = n >= args.min_trades and total > 0 and pf >= args.min_pf
        rows.append((v["id"], n, total, pf, ok))
        if ok:
            live.append({"id": v["id"], "score": v["score"], "forward": {"n": n, "net": total, "pf": pf}})

    print(f"{'variant':<64} {'fwd n':>6} {'fwd net':>10} {'pf':>6}  live")
    for vid, n, total, pf, ok in sorted(rows, key=lambda r: -r[2])[:25]:
        print(f"{vid:<64} {n:>6} {total:>10,.0f} {pf:>6.2f}  {'YES' if ok else '-'}")
    doc = {"generated_at": dt.datetime.now(IST).isoformat(), "source": "promote",
           "valid_until": str(dt.date.today() + dt.timedelta(days=args.valid_days)), "variants": live}
    path = os.path.join(args.data_dir, "approved_live.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"\n{len(live)} variants promoted -> {path}"
          + ("" if live else "  (none qualified: the live bot will not trade; keep paper trading)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
