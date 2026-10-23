"""SQLite recorder: market state, full option chain, decisions, trades.

The option chain is rebuilt every second (stream-built); the full chain is stored every
10 s (CHAIN_RECORD_EVERY_S) and ATM+/-5 strikes every second in table `flow`
(CHAIN_INTERVAL_S, FLOW_RECORD_EVERY_S). Chain rows carry IV and Greeks, so you accumulate your own historical option
data, which Kite does not provide for expired contracts. Roughly 160 rows per
snapshot: about 0.5-0.7M rows per trading day at 10 s. Archive/export old
days periodically (see the export command below).

Export:  python recorder.py export --table chain --since 2026-10-22 --out chain.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sqlite3
import threading
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS market(
  ts TEXT, spot REAL, vix REAL, regime TEXT, score INTEGER,
  atr_pct REAL, rv REAL, spike INTEGER, trend INTEGER);
CREATE TABLE IF NOT EXISTS chain(
  ts TEXT, expiry TEXT, strike REAL, kind TEXT, ltp REAL, oi REAL,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL);
CREATE TABLE IF NOT EXISTS events(
  ts TEXT, kind TEXT, variant TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS trades(
  ts TEXT, mode TEXT, variant TEXT, event TEXT, reason TEXT,
  gross REAL, cost REAL, net REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS shadow_trades(
  variant TEXT, entry_ts TEXT, exit_ts TEXT, reason TEXT, regime TEXT,
  gross REAL, cost REAL, net REAL, max_loss REAL);
CREATE TABLE IF NOT EXISTS features(
  ts TEXT, pcr REAL, call_wall REAL, put_wall REAL, max_pain REAL, net_gex REAL, atm_iv REAL,
  skew REAL, call_oi_chg REAL, put_oi_chg REAL, pcr_chg REAL, atm_spread_pct REAL, atm_volume REAL);
CREATE TABLE IF NOT EXISTS flow(
  ts TEXT, symbol TEXT, strike REAL, kind TEXT, ltp REAL, oi REAL, volume REAL, bid REAL, ask REAL);
CREATE TABLE IF NOT EXISTS trend(
  ts TEXT, d_dir INTEGER, d_valid INTEGER, d_trap TEXT, d_er REAL,
  i_dir INTEGER, i_valid INTEGER, i_trap TEXT, i_er REAL);
CREATE TABLE IF NOT EXISTS scan(
  ts TEXT, setup TEXT, bias INTEGER, score REAL, evidence TEXT);
CREATE INDEX IF NOT EXISTS ix_features_ts ON features(ts);
CREATE INDEX IF NOT EXISTS ix_flow_ts ON flow(ts);
CREATE INDEX IF NOT EXISTS ix_market_ts ON market(ts);
CREATE INDEX IF NOT EXISTS ix_chain_ts ON chain(ts);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_shadow_variant ON shadow_trades(variant);
"""

TABLES = ("market", "chain", "features", "flow", "trend", "scan", "events", "trades", "shadow_trades")


def _num(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return None if (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x


class Recorder:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def _write(self, sql: str, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            try:
                self._db.execute("BEGIN")
                self._db.executemany(sql, rows)
                self._db.execute("COMMIT")
            except sqlite3.Error:
                log.exception("Recorder write failed")
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

    # ------------------------------------------------------------------ writes
    def record_snapshot(self, snap: Any, with_chain: bool = True) -> None:
        ts = snap.ts.isoformat(timespec="seconds")
        rs = snap.rs
        self._write("INSERT INTO market VALUES (?,?,?,?,?,?,?,?,?)", [(
            ts, snap.spot, snap.vix, rs.regime.value, rs.score,
            _num(rs.atr_pct), _num(rs.realized_vol), int(rs.spike), snap.trend_dir)])
        ti = getattr(snap, "tinfo", None)
        if ti is not None and with_chain:
            d, i = ti.daily, ti.intraday
            self._write("INSERT INTO trend VALUES (?,?,?,?,?,?,?,?,?)", [(
                ts, d.dir, int(d.valid), d.trap, d.er,
                *((i.dir, int(i.valid), i.trap, i.er) if i else (None, None, None, None)))])
        if with_chain:
            try:
                from scanner import scan
                rows = [(ts, o.setup, o.bias, o.score, o.evidence) for o in scan(snap)[:3]]
                if rows:
                    self._write("INSERT INTO scan VALUES (?,?,?,?,?)", rows)
            except Exception:  # noqa: BLE001 - the scanner is advisory; never block data recording
                if not getattr(self, "_scan_err_logged", False):
                    self._scan_err_logged = True
                    import logging
                    logging.getLogger("recorder").exception("Scanner failed (logged once)")
        if snap.chain is not None and with_chain:
            rows = []
            exp = snap.chain.expiry.isoformat()
            for (strike, kind), q in snap.chain.quotes.items():
                g = q.greeks
                rows.append((ts, exp, strike, kind, q.ltp, q.oi, _num(q.iv),
                             g.delta if g else None, g.gamma if g else None,
                             g.theta if g else None, g.vega if g else None))
            self._write("INSERT INTO chain VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        f = getattr(snap, "features", None)
        if f is not None:
            self._write("INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [(
                ts, _num(f.pcr), f.call_wall, f.put_wall, f.max_pain, _num(f.net_gex), _num(f.atm_iv),
                _num(f.skew), _num(f.call_oi_chg), _num(f.put_oi_chg), _num(f.pcr_chg),
                _num(f.atm_spread_pct), f.atm_volume)])

    def flow(self, rows: list[tuple]) -> None:
        """rows: (ts, symbol, strike, kind, ltp, oi, volume, bid, ask) sampled from the WebSocket stream."""
        self._write("INSERT INTO flow VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def event(self, ts: str, kind: str, variant: str, payload: dict) -> None:
        self._write("INSERT INTO events VALUES (?,?,?,?)",
                    [(ts, kind, variant, json.dumps(payload, default=str))])

    def trade(self, ts: str, mode: str, variant: str, event: str, reason: str,
              gross: float, cost: float, net: float, payload: dict) -> None:
        self._write("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)", [(
            ts, mode, variant, event, reason, gross, cost, net, json.dumps(payload, default=str))])

    def shadow_trade(self, variant: str, entry_ts: str, exit_ts: str, reason: str,
                     regime: str, gross: float, cost: float, net: float, max_loss: float) -> None:
        self._write("INSERT INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?)", [(
            variant, entry_ts, exit_ts, reason, regime, gross, cost, net, max_loss)])

    def prune_flow(self, keep_days: int, today: str) -> int:
        """Delete flow rows older than keep_days (the 1 s capture is the bulky table). 0 = keep everything."""
        if keep_days <= 0:
            return 0
        import datetime as _dt
        cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=keep_days)).isoformat()
        with self._lock:
            try:
                n = self._db.execute("DELETE FROM flow WHERE ts < ?", (cutoff,)).rowcount
                self._db.commit()
                return n
            except sqlite3.Error:
                log.exception("Flow prune failed")
                return 0

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _export(db_path: str, table: str, since: Optional[str], out: str) -> int:
    if table not in TABLES:
        raise SystemExit(f"table must be one of {TABLES}")
    db = sqlite3.connect(db_path)
    col = {"shadow_trades": "entry_ts"}.get(table, "ts")
    sql = f"SELECT * FROM {table}" + (f" WHERE {col} >= ?" if since else "")
    cur = db.execute(sql, (since,) if since else ())
    n = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([d[0] for d in cur.description])
        for row in cur:
            w.writerow(row)
            n += 1
    db.close()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Recorder utilities")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--db", default=os.path.join(os.getenv("DATA_DIR", "data"), "sxbot.db"))
    ex.add_argument("--table", required=True)
    ex.add_argument("--since", help="ISO date/time lower bound, e.g. 2026-10-22")
    ex.add_argument("--out", required=True)
    args = ap.parse_args()
    print(f"exported {_export(args.db, args.table, args.since, args.out)} rows to {args.out}")
