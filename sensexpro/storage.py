"""SQLite journal (brief section 11) with numbered schema migrations.

One file (data/sxbot.db by default, DATABASE_URL=sqlite:///path to change it) shared with the existing recorder, whose
`trades` / `events` tables feed the report page. Timestamps are stored as UTC ISO-8601. Every write is committed
immediately (WAL), so a crash never loses an order intent that was about to be sent. The repository API is small and
SQL-standard, so moving to PostgreSQL later only replaces this module.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Iterable, Optional

from .models import Bar, Position, from_utc_iso, utc_iso

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # 1: initial SENSEX PRO schema
    """
    CREATE TABLE IF NOT EXISTS sp_candles(idx TEXT, tf INTEGER, start TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, gap INTEGER, PRIMARY KEY(idx, tf, start));
    CREATE TABLE IF NOT EXISTS sp_quotes(ts TEXT, symbol TEXT, bid REAL, ask REAL, bid_qty INTEGER, ask_qty INTEGER,
        ltp REAL, volume REAL, oi REAL, avg_price REAL, exchange_ts TEXT);
    CREATE INDEX IF NOT EXISTS ix_sp_quotes_ts ON sp_quotes(ts);
    CREATE TABLE IF NOT EXISTS sp_signals(ts TEXT, idx TEXT, bar_end TEXT, strategy TEXT, direction TEXT,
        setup_key TEXT, regime TEXT, score REAL, decision TEXT, components TEXT, reasons TEXT, evidence TEXT);
    CREATE INDEX IF NOT EXISTS ix_sp_signals_ts ON sp_signals(ts);
    CREATE TABLE IF NOT EXISTS sp_consumed(day TEXT, setup_key TEXT, why TEXT, PRIMARY KEY(day, setup_key));
    CREATE TABLE IF NOT EXISTS sp_selections(ts TEXT, idx TEXT, setup_key TEXT, symbol TEXT, lots INTEGER,
        outlay REAL, spread_pct REAL, note TEXT, alternatives TEXT, rejections TEXT);
    CREATE TABLE IF NOT EXISTS sp_orders(client_id TEXT PRIMARY KEY, mode TEXT, position_id TEXT, idx TEXT,
        symbol TEXT, token INTEGER, exchange TEXT, side TEXT, kind TEXT, requested_qty INTEGER, filled_qty INTEGER,
        avg_price REAL, limit_price REAL, state TEXT, reason TEXT, broker_order_id TEXT, emergency INTEGER,
        created TEXT, updated TEXT);
    CREATE TABLE IF NOT EXISTS sp_order_events(ts TEXT, client_id TEXT, state TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS sp_fills(ts TEXT, client_id TEXT, qty INTEGER, price REAL);
    CREATE TABLE IF NOT EXISTS sp_positions(id TEXT PRIMARY KEY, mode TEXT, status TEXT, idx TEXT, symbol TEXT,
        opened TEXT, closed TEXT, gross REAL, cost REAL, net REAL, exit_reason TEXT, data TEXT);
    CREATE TABLE IF NOT EXISTS sp_exit_assessments(ts TEXT, position_id TEXT, total REAL, action TEXT,
        components TEXT, missing TEXT, reasons TEXT, structural INTEGER, emergency INTEGER);
    CREATE TABLE IF NOT EXISTS sp_risk_events(ts TEXT, mode TEXT, kind TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS sp_state(mode TEXT, key TEXT, value TEXT, PRIMARY KEY(mode, key));
    CREATE TABLE IF NOT EXISTS sp_app_events(ts TEXT, kind TEXT, detail TEXT);
    """,
]

TERMINAL = ("FILLED", "CANCELLED", "REJECTED")


def db_path_from_env(default: str) -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return default


def _now() -> str:
    return utc_iso(dt.datetime.now(dt.timezone.utc))


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._lock = threading.RLock()
        self.migrate()

    # ------------------------------------------------------------------ plumbing
    def migrate(self) -> int:
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS sp_schema(version INTEGER)")
            row = self._db.execute("SELECT MAX(version) FROM sp_schema").fetchone()
            cur = row[0] or 0
            for n, sql in enumerate(MIGRATIONS, start=1):
                if n <= cur:
                    continue
                self._db.execute("BEGIN")
                try:
                    for stmt in [s for s in sql.split(";") if s.strip()]:
                        self._db.execute(stmt)
                    self._db.execute("INSERT INTO sp_schema VALUES (?)", (n,))
                    self._db.execute("COMMIT")
                except sqlite3.Error:
                    self._db.execute("ROLLBACK")
                    raise
                log.info("Database migrated to schema version %d", n)
                cur = n
            return cur

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _many(self, sql: str, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(sql, rows)
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._db.execute("ROLLBACK")
                raise

    def backup(self, dest: str) -> None:
        """Consistent online copy (sqlite backup API)."""
        with self._lock:
            out = sqlite3.connect(dest)
            try:
                self._db.backup(out)
            finally:
                out.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ market data
    def save_candles(self, index: str, bars: Iterable[Bar]) -> None:
        self._many("INSERT OR REPLACE INTO sp_candles VALUES (?,?,?,?,?,?,?,?,?)",
                   ((index, b.minutes, utc_iso(b.start), b.open, b.high, b.low, b.close, b.volume, int(b.gap))
                    for b in bars))

    def quotes(self, rows: Iterable[tuple]) -> None:
        self._many("INSERT INTO sp_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    # ------------------------------------------------------------------ signals
    def signal(self, ts: dt.datetime, sig, decision: Optional[str] = None) -> None:
        c = sig.candidate
        self._x("INSERT INTO sp_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (utc_iso(ts), c.index, utc_iso(c.bar_end), c.strategy, c.direction, c.setup_key, sig.regime,
                 sig.score, decision or sig.decision, json.dumps(sig.components), json.dumps(list(sig.reasons)),
                 json.dumps(c.evidence, default=str)))

    def consume(self, day: dt.date, setup_key: str, why: str) -> None:
        self._x("INSERT OR IGNORE INTO sp_consumed VALUES (?,?,?)", (day.isoformat(), setup_key, why))

    def consumed(self, day: dt.date) -> set[str]:
        return {r[0] for r in self._x("SELECT setup_key FROM sp_consumed WHERE day=?", (day.isoformat(),))}

    def selection(self, ts: dt.datetime, index: str, setup_key: str, sel, rejections: list) -> None:
        self._x("INSERT INTO sp_selections VALUES (?,?,?,?,?,?,?,?,?,?)",
                (utc_iso(ts), index, setup_key, sel.contract.symbol if sel else None, sel.lots if sel else None,
                 sel.outlay if sel else None, sel.spread_pct if sel else None, sel.note if sel else "none passed",
                 json.dumps(list(sel.alternatives) if sel else []), json.dumps(rejections)))

    # ------------------------------------------------------------------ orders
    def order_intent(self, o: dict) -> None:
        """Persist an order BEFORE it goes to the broker (idempotency: client_id is the primary key)."""
        now = _now()
        self._x("INSERT INTO sp_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["client_id"], o["mode"], o["position_id"], o["index"], o["symbol"], o["token"], o["exchange"],
                 o["side"], o["kind"], o["requested_qty"], 0, None, o.get("limit_price"), "CREATED", o.get("reason", ""),
                 None, int(bool(o.get("emergency"))), now, now))
        self._x("INSERT INTO sp_order_events VALUES (?,?,?,?)", (now, o["client_id"], "CREATED", ""))

    def order_update(self, client_id: str, state: str, detail: str = "", **fields: Any) -> None:
        sets = ["state=?", "updated=?"]
        args: list = [state, _now()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(v)
        args.append(client_id)
        self._x(f"UPDATE sp_orders SET {', '.join(sets)} WHERE client_id=?", tuple(args))
        self._x("INSERT INTO sp_order_events VALUES (?,?,?,?)", (_now(), client_id, state, detail[:500]))

    def fill(self, client_id: str, qty: int, price: float) -> None:
        self._x("INSERT INTO sp_fills VALUES (?,?,?,?)", (_now(), client_id, qty, price))

    def order(self, client_id: str) -> Optional[dict]:
        cur = self._x("SELECT * FROM sp_orders WHERE client_id=?", (client_id,))
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def open_orders(self, mode: str) -> list[dict]:
        cur = self._x(f"SELECT * FROM sp_orders WHERE mode=? AND state NOT IN {TERMINAL}", (mode,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def orders_on(self, mode: str, day_utc_prefix: str) -> list[dict]:
        cur = self._x("SELECT * FROM sp_orders WHERE mode=? AND created>=? ORDER BY created", (mode, day_utc_prefix))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def next_order_seq(self, prefix: str) -> int:
        row = self._x("SELECT COUNT(*) FROM sp_orders WHERE client_id LIKE ?", (prefix + "%",)).fetchone()
        return int(row[0]) + 1

    # ------------------------------------------------------------------ positions
    def save_position(self, mode: str, pos: Position, gross: Optional[float] = None, cost: Optional[float] = None,
                      net: Optional[float] = None, exit_reason: Optional[str] = None) -> None:
        closed = _now() if pos.status == "CLOSED" else None
        self._x("INSERT INTO sp_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, closed=COALESCE(excluded.closed, sp_positions.closed), "
                "gross=COALESCE(excluded.gross, sp_positions.gross), cost=COALESCE(excluded.cost, sp_positions.cost), "
                "net=COALESCE(excluded.net, sp_positions.net), "
                "exit_reason=COALESCE(excluded.exit_reason, sp_positions.exit_reason), data=excluded.data",
                (pos.id, mode, pos.status, pos.index, pos.contract.symbol, utc_iso(pos.entry_time), closed, gross,
                 cost, net, exit_reason, json.dumps(pos.to_dict(), default=str)))

    def open_position(self, mode: str) -> Optional[Position]:
        row = self._x("SELECT data FROM sp_positions WHERE mode=? AND status IN ('PENDING','OPEN','CLOSING') "
                      "ORDER BY opened DESC LIMIT 1", (mode,)).fetchone()
        return Position.from_dict(json.loads(row[0])) if row else None

    def position_fills(self, position_id: str) -> list[tuple[str, str, int, float]]:
        """(order kind, side, qty, price) of every journaled fill of this position, in time order."""
        return [tuple(r) for r in self._x(
            "SELECT o.kind, o.side, f.qty, f.price FROM sp_fills f JOIN sp_orders o ON o.client_id=f.client_id "
            "WHERE o.position_id=? ORDER BY f.rowid", (position_id,)).fetchall()]

    def closed_positions(self, mode: str, since_utc: str) -> list[dict]:
        cur = self._x("SELECT id, idx, symbol, opened, closed, gross, cost, net, exit_reason FROM sp_positions "
                      "WHERE mode=? AND status='CLOSED' AND closed>=? ORDER BY closed", (mode, since_utc))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------ exits / risk / state
    def exit_assessment(self, position_id: str, a) -> None:
        self._x("INSERT INTO sp_exit_assessments VALUES (?,?,?,?,?,?,?,?,?)",
                (utc_iso(a.at), position_id, a.total, a.action, json.dumps(a.components), json.dumps(list(a.missing)),
                 json.dumps(list(a.reasons)), int(a.structural), int(a.emergency)))

    def risk_event(self, mode: str, kind: str, detail: dict) -> None:
        self._x("INSERT INTO sp_risk_events VALUES (?,?,?,?)", (_now(), mode, kind, json.dumps(detail, default=str)))

    def app_event(self, kind: str, detail: dict) -> None:
        self._x("INSERT INTO sp_app_events VALUES (?,?,?)", (_now(), kind, json.dumps(detail, default=str)))

    def get_state(self, mode: str, key: str, default: Any = None) -> Any:
        row = self._x("SELECT value FROM sp_state WHERE mode=? AND key=?", (mode, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, mode: str, key: str, value: Any) -> None:
        self._x("INSERT INTO sp_state VALUES (?,?,?) ON CONFLICT(mode, key) DO UPDATE SET value=excluded.value",
                (mode, key, json.dumps(value, default=str)))

    def last_rows(self, table: str, n: int = 20, where: str = "", args: tuple = ()) -> list[dict]:
        allowed = {"sp_signals", "sp_orders", "sp_positions", "sp_exit_assessments", "sp_risk_events",
                   "sp_selections", "sp_app_events"}
        if table not in allowed:
            raise ValueError(table)
        cur = self._x(f"SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT ?", args + (n,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    return from_utc_iso(s)
