"""Operator CLI (read-only except reset-kill).

    python -m sensexpro.cli health          process-independent health: mode, DB, last candle, kill switch, open orders
    python -m sensexpro.cli status          the risk book and open position from the journal
    python -m sensexpro.cli reset-kill --mode PAPER|LIVE   reset a latched kill switch (after review)
    python -m sensexpro.cli check-config    validate config/*.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from .models import IST
from .risk import RiskBook
from .settings import ConfigError, load_settings
from .storage import Store, db_path_from_env


def _store() -> Store:
    return Store(db_path_from_env(os.path.join(os.getenv("DATA_DIR", "data"), "sxbot.db")))


def _mode() -> str:
    try:
        with open(os.path.join(os.getenv("DATA_DIR", "data"), "mode.json"), encoding="utf-8") as f:
            return str(json.load(f).get("mode", "PAPER")).upper()
    except (OSError, ValueError):
        return "PAPER"


def health() -> int:
    st = _store()
    mode = _mode()
    book = RiskBook.restore(st.get_state(mode, "risk_book"))
    now = dt.datetime.now(IST)
    last = st._x("SELECT idx, MAX(start) FROM sp_candles GROUP BY idx").fetchall()
    out = {"now": now.isoformat(timespec="seconds"), "mode_file": mode,
           "live_master_switch": os.getenv("LIVE_TRADING_CONFIRM") == "YES",
           "kill_switch": {"latched": book.kill_latched, "reason": book.kill_reason},
           "day": book.day, "day_pnl": book.day_realized, "entries_today": book.entries_today,
           "open_orders": len(st.open_orders(mode)),
           "open_position": (lambda p: p and {"symbol": p.contract.symbol, "qty": p.qty, "status": p.status})(
               st.open_position(mode)),
           "last_candle_utc": {i: t for i, t in last}}
    print(json.dumps(out, indent=1, default=str))
    return 1 if book.kill_latched or out["open_orders"] else 0


def status() -> int:
    st = _store()
    mode = _mode()
    print(json.dumps({"risk_book": st.get_state(mode, "risk_book"),
                      "recent_positions": st.last_rows("sp_positions", 5, "WHERE mode=?", (mode,))},
                     indent=1, default=str))
    return 0


def reset_kill(mode: str) -> int:
    st = _store()
    book = RiskBook.restore(st.get_state(mode, "risk_book"))
    was = book.reset_kill()
    st.set_state(mode, "risk_book", book.export())
    st.risk_event(mode, "kill_reset", {"was": was, "by": "cli"})
    print(f"Kill switch reset for {mode} (was: {was or 'not latched'}). Restart the bot if it is running "
          "(docker compose restart bot) so it reloads the book.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sensexpro")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("status")
    rk = sub.add_parser("reset-kill")
    rk.add_argument("--mode", required=True, choices=["PAPER", "LIVE"])
    sub.add_parser("check-config")
    a = ap.parse_args(argv)
    if a.cmd == "health":
        return health()
    if a.cmd == "status":
        return status()
    if a.cmd == "reset-kill":
        return reset_kill(a.mode)
    try:
        s = load_settings()
    except ConfigError as exc:
        print(f"INVALID: {exc}")
        return 2
    print(f"OK: {len(s.indices)} indices, premium cap {s.risk.max_premium_per_entry} at reference capital "
          f"{s.risk.reference_capital}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
