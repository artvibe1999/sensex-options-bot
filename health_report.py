"""Is the bot working? Read-only report from data/sxbot.db and the approval files.

Run on the VM:  docker compose exec bot python health_report.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3

from config import IST, load_config
from token_store import is_fresh, read_token


def _q(con, sql, *a):
    return con.execute(sql, a).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="IST date, default today")
    args = ap.parse_args()
    cfg = load_config()
    now = dt.datetime.now(IST)
    day = args.date or now.date().isoformat()
    flags: list[str] = []
    print(f"=== SENSEX bot health | {now:%Y-%m-%d %H:%M} IST | report for {day} ===")

    rec = read_token(cfg.paths.token_file)
    print("Kite token      :", "fresh" if is_fresh(rec) else "MISSING/EXPIRED")
    if not is_fresh(rec):
        flags.append("Not logged in to Kite today: no session, no data.")

    for label, path in (("paper approvals", cfg.paths.approved_backtest), ("live approvals ", cfg.paths.approved_live)):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            print(f"{label}  : {len(d.get('variants', []))} variants, valid until {d.get('valid_until')}")
        except (OSError, ValueError):
            print(f"{label}  : none (file missing)")
            if label.startswith("paper"):
                flags.append("No approved variants: the bot records data and shadow-trades but places NO paper "
                             "orders. Run: python backtest.py --fetch-kite 700  (then restart the bot).")

    if not os.path.exists(cfg.paths.db):
        print("database        : not created yet")
        return 1
    con = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
    like = day + "%"
    m = _q(con, "SELECT COUNT(*), MIN(ts), MAX(ts) FROM market WHERE ts LIKE ?", like)[0]
    c = _q(con, "SELECT COUNT(*), MAX(ts) FROM chain WHERE ts LIKE ?", like)[0]
    print(f"market snapshots: {m[0]}  first {m[1]}  last {m[2]}")
    print(f"chain rows      : {c[0]}  last {c[1]}")
    if m[0]:
        last = _q(con, "SELECT ts, spot, vix, regime, score, spike FROM market WHERE ts LIKE ? ORDER BY ts DESC LIMIT 1", like)[0]
        print(f"latest market   : spot {last[1]:.0f}  VIX {last[2]}  regime {last[3]}  score {last[4]}  spike {bool(last[5])}")
        age = (now - dt.datetime.fromisoformat(last[0])).total_seconds()
        in_hours = day == now.date().isoformat() and dt.time(9, 20) <= now.time() <= dt.time(15, 30) and now.weekday() < 5
        if in_hours and age > 120:
            flags.append(f"Data is stale: last snapshot {age:.0f}s ago during market hours.")
    elif day == now.date().isoformat() and now.time() > dt.time(9, 30) and now.weekday() < 5:
        flags.append("No market data recorded today (holiday, no login, or the stream is down).")
    if c[0] == 0 and m[0]:
        flags.append("Spot recorded but option chain empty: check expiry/instrument mapping in the bot logs.")

    ev = _q(con, "SELECT kind, COUNT(*) FROM events WHERE ts LIKE ? GROUP BY kind", like)
    print("events today    :", dict(ev) or "none")
    tr = _q(con, "SELECT ts, mode, variant, event, reason, net FROM trades WHERE ts LIKE ? ORDER BY ts", like)
    print(f"real/paper trades today: {len(tr)}")
    for t in tr:
        print("   ", t)

    s = _q(con, "SELECT COUNT(*), COALESCE(SUM(net),0), COALESCE(SUM(net>0),0) FROM shadow_trades WHERE entry_ts LIKE ?", like)[0]
    print(f"shadow trades   : {s[0]} closed today, net {s[1]:,.0f}, winners {s[2]}  (these are simulated for all variants)")
    if s[0]:
        for name, order in (("best ", "DESC"), ("worst", "ASC")):
            r = _q(con, f"SELECT variant, net FROM shadow_trades WHERE entry_ts LIKE ? ORDER BY net {order} LIMIT 2", like)
            print(f"   {name}:", [(v[:48], round(n)) for v, n in r])
    tot = _q(con, "SELECT COUNT(*) FROM shadow_trades")[0][0]
    days = _q(con, "SELECT COUNT(DISTINCT substr(ts,1,10)) FROM market")[0][0]
    print(f"history         : {days} recorded day(s), {tot} shadow trades in total")
    if flags:
        print("\nATTENTION:")
        for f in flags:
            print(" -", f)
        return 1
    print("\nOK: no problems detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
