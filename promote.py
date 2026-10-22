"""Promote variants to live: backtest approval AND forward (shadow) evidence.

  python promote.py                      # defaults: >=20 forward trades, PF >= 1.2, net > 0
  python promote.py --min-trades 30 --min-pf 1.3

OI/gamma-rule variants (ids ending in |oi=<rule>) cannot be backtested (no historical OI). They get a
STRICTER forward-only test: more trades, higher profit factor, AND they must beat their plain twin
(same trade without the filter) on both average net per trade and profit factor over the same period.

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
from strategy_library import build_library


def _pf(nets: list[float]) -> float:
    wins, losses = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    return 99.0 if losses == 0 and wins > 0 else (wins / losses if losses else 0.0)


def main() -> int:
    data_dir = os.getenv("DATA_DIR", "data")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=data_dir)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--min-pf", type=float, default=1.2)
    ap.add_argument("--min-trades-oi", type=int, default=40, help="forward trades needed by OI-rule variants")
    ap.add_argument("--min-pf-oi", type=float, default=1.3)
    ap.add_argument("--lookback-days", type=int, default=90, help="forward window considered")
    ap.add_argument("--valid-days", type=int, default=30)
    args = ap.parse_args()

    try:
        with open(os.path.join(args.data_dir, "approved_backtest.json"), encoding="utf-8") as f:
            bt = json.load(f)
    except OSError:
        print("No approved_backtest.json: only OI-rule variants can be promoted.")
        bt = {"variants": []}
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

    oi_rows = []
    for spec in build_library():
        if not spec.needs_oi:
            continue
        nets, base = fwd.get(spec.id, []), fwd.get(spec.base_id, [])
        n, total, pf = len(nets), sum(nets), _pf(nets)
        bn = len(base)
        avg, bavg, bpf = (total / n if n else 0.0), (sum(base) / bn if bn else 0.0), (_pf(base) if bn else 0.0)
        ok = (n >= args.min_trades_oi and total > 0 and pf >= args.min_pf_oi
              and bn >= 20 and avg > bavg and pf > bpf)
        oi_rows.append((spec.id, n, total, pf, bn, bavg, ok))
        if ok:
            live.append({"id": spec.id, "score": pf, "evidence": "forward_only",
                         "forward": {"n": n, "net": total, "pf": pf},
                         "plain_twin": {"n": bn, "avg": bavg, "pf": bpf}})

    print(f"{'variant':<64} {'fwd n':>6} {'fwd net':>10} {'pf':>6}  live")
    for vid, n, total, pf, ok in sorted(rows, key=lambda r: -r[2])[:25]:
        print(f"{vid:<64} {n:>6} {total:>10,.0f} {pf:>6.2f}  {'YES' if ok else '-'}")
    if any(r[1] for r in oi_rows):
        print("\nOI/gamma-rule variants (forward-only; must beat the plain twin):")
        print(f"{'variant':<72} {'n':>4} {'net':>9} {'pf':>5} {'twin n':>6} {'twin avg':>9}  live")
        for vid, n, total, pf, bn, bavg, ok in sorted(oi_rows, key=lambda r: -r[2])[:15]:
            if n:
                print(f"{vid:<72} {n:>4} {total:>9,.0f} {pf:>5.2f} {bn:>6} {bavg:>9,.0f}  {'YES' if ok else '-'}")
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
