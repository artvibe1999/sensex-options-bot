"""Health summary: the Google Sheet "Health" tab row (every 15 minutes) and a CLI check.

    docker compose exec bot python health_report.py     # today's health facts and problems (database read-only)

The Health tab keeps its original columns; for SENSEX PRO they mean: "Approved variants" = number of entry strategies
running, "Market rows today" = 1-minute candles stored today (all indices), "Chain rows today" = option quote
snapshots stored today, "Shadow trades closed" = positions closed today, "Shadow net P&L" = today's net P&L.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from typing import Any, Optional

from config import IST, Config, load_config
from token_store import is_fresh, read_token

HEALTH_HEADER = ["Time (IST)", "Mode", "Kite login", "Approved variants", "SENSEX spot", "India VIX", "Regime",
                 "Market rows today", "Chain rows today", "Data age (s)", "Trades today",
                 "Shadow trades closed", "Shadow net P&L", "Status / problems"]


def _q(con, sql, *a):
    try:
        return con.execute(sql, a).fetchall()
    except sqlite3.OperationalError:
        return []


def collect(cfg: Config, now: dt.datetime, status: Optional[dict] = None) -> dict[str, Any]:
    """Health facts (never raises on a missing DB). `status` = TradingEngine.status() when called from the bot."""
    flags: list[str] = []
    day = now.date().isoformat()
    day_utc = dt.datetime.combine(now.date(), dt.time(0, 0), IST).astimezone(dt.timezone.utc).isoformat()
    info: dict[str, Any] = {"day": day, "token_fresh": is_fresh(read_token(cfg.paths.token_file)),
                            "strategies": 5, "spot": None, "vix": None, "regime": None, "candles": 0, "quotes": 0,
                            "age_s": None, "entries": 0, "closed": 0, "net": 0.0}
    if not info["token_fresh"]:
        flags.append("Not logged in to Kite today: no session, no data.")
    if status:
        sx = (status.get("indices") or {}).get("SENSEX") or {}
        info.update(spot=sx.get("spot"), regime=sx.get("regime"), entries=status["book"]["entries_today"],
                    net=status["book"]["day_pnl"])
        if status["kill_switch"]["latched"]:
            flags.append(f"KILL SWITCH LATCHED: {status['kill_switch']['reason']}")
        if status.get("day_block"):
            flags.append(f"Entries blocked today: {status['day_block']}")
        bad = [k for k, v in (status.get("indices") or {}).items() if not v.get("data_ok")]
        in_hours = now.weekday() < 5 and dt.time(9, 20) <= now.time() <= dt.time(15, 30)
        if bad and in_hours:
            flags.append("Data not ok: " + ", ".join(bad))
    if os.path.exists(cfg.paths.db):
        con = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True, timeout=5)
        try:
            info["candles"] = (_q(con, "SELECT COUNT(*) FROM sp_candles WHERE start>=?", day_utc) or [[0]])[0][0]
            info["quotes"] = (_q(con, "SELECT COUNT(*) FROM sp_quotes WHERE ts>=?", day_utc) or [[0]])[0][0]
            last = _q(con, "SELECT MAX(start) FROM sp_candles WHERE start>=?", day_utc)
            if last and last[0][0]:
                info["age_s"] = round((now - dt.datetime.fromisoformat(last[0][0])).total_seconds() - 60)
            r = _q(con, "SELECT COUNT(*), COALESCE(SUM(net),0) FROM sp_positions WHERE status='CLOSED' AND closed>=?",
                   day_utc)
            if r:
                info["closed"] = r[0][0]
                if not status:
                    info["net"] = round(r[0][1], 2)
            if not status:
                info["entries"] = (_q(con, "SELECT COUNT(*) FROM sp_orders WHERE kind='entry' AND filled_qty>0 "
                                           "AND created>=?", day_utc) or [[0]])[0][0]
        finally:
            con.close()
    else:
        flags.append("Database not created yet.")
    return {"info": info, "flags": flags}


def sheet_row(now: dt.datetime, mode: str, res: dict[str, Any]) -> list:
    i, f = res["info"], res["flags"]
    return [now.isoformat(timespec="seconds"), mode, "yes" if i["token_fresh"] else "NO", i["strategies"],
            i["spot"], i["vix"], i["regime"], i["candles"], i["quotes"], i["age_s"], i["entries"], i["closed"],
            round(i["net"] or 0), "OK" if not f else " | ".join(f)[:300]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    cfg = load_config()
    res = collect(cfg, dt.datetime.now(IST))
    print(json.dumps(res, indent=1, default=str))
    return 0 if not res["flags"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
