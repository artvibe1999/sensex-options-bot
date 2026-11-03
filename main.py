"""SENSEX PRO entry point: python main.py --auto | --paper | --live

Long-lived daemon (Docker restart policy). Each trading day it logs in with the token written by token_service.py,
loads the instrument master, streams the six indices and their option windows, and runs the SENSEX PRO trading loop
(sensexpro/engine.py): five entry strategies, regime filter, scored ensemble, contract selection, risk approval,
adaptive exits with independent emergency protection. PAPER by default; LIVE only after Telegram /mode live + /confirm
AND LIVE_TRADING_CONFIRM=YES in .env.

Kept from the earlier bot: Kite login (token_service), Telegram alerts and commands, the Google Sheet feed (Orders /
Trades / Status / Health / Log tabs, same columns), the report page on Cloud Storage, data/state.json for the VM's
autostop guard.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

from kiteconnect import exceptions as kex

from alerts import Alerter
from config import IST, Config, load_config
from health_report import HEALTH_HEADER, collect as health_collect, sheet_row as health_row
from indices import INDICES
from mode_control import live_allowed, startup_mode, write_mode
from recorder import Recorder
from report_site import ReportSite
from sensexpro.dashboard import Dashboard
from sensexpro.engine import TradingEngine
from sensexpro.execution import LiveBroker, PaperBroker
from sensexpro.market import IndexMarket
from sensexpro.settings import ConfigError, Settings, load_settings
from sensexpro.storage import Store, db_path_from_env
from sheet_log import SheetLogHandler
from sheets_sink import ORDERS_HEADER, STATUS_HEADER, TRADES_HEADER, SheetsSink
from telegram_control import TelegramControl
from zerodha_client import AuthError, ZerodhaClient

log = logging.getLogger("bot")


class BotNotifier:
    """Engine events -> Telegram, Google Sheet, report journal (recorder `trades` table)."""

    def __init__(self, alerts: Alerter, sheets: SheetsSink, rec: Recorder, mode: str) -> None:
        self.alerts, self.sheets, self.rec, self.mode = alerts, sheets, rec, mode

    def alert(self, text: str, level: str = "INFO", key: Optional[str] = None) -> None:
        self.alerts.send(text, level=level, key=key)

    def order_row(self, row: list) -> None:
        self.sheets.append("Orders", ORDERS_HEADER, [row])

    def trade_row(self, row: list) -> None:
        self.sheets.append("Trades", TRADES_HEADER, [row])

    def status_row(self, event: str, details: str) -> None:
        self.sheets.append("Status", STATUS_HEADER, [[dt.datetime.now(IST).isoformat(timespec="seconds"), self.mode,
                                                      event, details]])

    def journal_trade(self, ts: str, variant: str, event: str, reason: str, gross: float, cost: float, net: float,
                      payload: dict) -> None:
        self.rec.trade(ts, self.mode, variant, event, reason, gross, cost, net, payload)
        self.rec.event(ts, event, variant, payload)


class Bot:
    def __init__(self, cfg: Config, s: Settings, live: bool) -> None:
        self.cfg, self.s, self.live = cfg, s, live
        self.mode = "LIVE" if live else "PAPER"
        self.lock = threading.RLock()                 # engine state: main loop vs Telegram / dashboard threads
        self.client = ZerodhaClient(cfg.credentials, cfg.paths.token_file)
        self.alerts = Alerter(cfg.alerts.telegram_token, cfg.alerts.telegram_chat_id, background=True)
        self.rec = Recorder(cfg.paths.db)
        self.store = Store(db_path_from_env(cfg.paths.db))
        self.sheets = SheetsSink(cfg.sheets_url, cfg.sheets_secret)
        self.sheet_log = SheetLogHandler(self.sheets, self.mode)
        logging.getLogger().addHandler(self.sheet_log)
        if self.sheets.enabled:
            self.sheet_log.start()
        self.stop = threading.Event()
        self.markets = {n: IndexMarket(INDICES[n], self.client, s, self.store) for n in s.indices}
        quote_fn = self._quote
        broker = LiveBroker(self.client, s.execution.live, quote_fn) if live else \
            PaperBroker(s.execution.paper, quote_fn, s.data.quote_stale_s)
        self.notifier = BotNotifier(self.alerts, self.sheets, self.rec, self.mode)
        tok, bucket = os.getenv("REPORT_TOKEN", "").strip(), os.getenv("REPORT_GCS_BUCKET", "").strip()
        self.report = ReportSite(cfg.paths.data_dir, os.getenv("REPORT_SINCE", "2026-11-02"), cfg.costs,
                                 os.getenv("REPORT_BACKTEST_URL", ""), bucket,
                                 f"r/{tok}/index.html" if bucket and tok else "")
        self.tg = TelegramControl(cfg.alerts.telegram_token, cfg.alerts.telegram_chat_id,
                                  os.path.join(cfg.paths.data_dir, "settings.json"),
                                  reply=lambda t: self.alerts.send(t), status_fn=self._status_text,
                                  default_single=INDICES["SENSEX"].default_lots, report_url=self.report.url,
                                  index_defaults={n: sp.default_lots for n, sp in INDICES.items() if n != "SENSEX"},
                                  indices_fn=self._indices_text, mode_fn=self._request_mode,
                                  mode_now=lambda: self.mode,
                                  admin_ids={x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
                                             if x.strip()},
                                  risk_fn=self._risk_text, signals_fn=self._signals_text,
                                  kill_fn=self._kill, reset_kill_fn=self._reset_kill)
        self.engine = TradingEngine(s, self.mode, self.store, self.markets, broker, cfg.costs, self.notifier,
                                    lots_fn=lambda ix: self.tg.lots_for(ix), max_lots=cfg.max_lots,
                                    broker_positions_fn=self.client.net_positions if live else None,
                                    state_file=cfg.paths.state)
        self.dashboard = Dashboard(s.dashboard.host, int(s.dashboard.port), self._dashboard_status) \
            if getattr(s.dashboard, "enabled", False) else None
        self._mode_path = os.path.join(cfg.paths.data_dir, "mode.json")
        self._switch_to: Optional[str] = None
        self.day: Optional[dt.date] = None
        self.session_ready = False
        self.eod_done = self.report_done = False
        self.market_seen_today = False
        self._auth_alerted = self._feed_alerted = False
        self._last_heartbeat = 0.0
        self.capital = 0.0
        self._load_day_flags()
        self.tg.start()
        if self.dashboard:
            self.dashboard.start()

    # ------------------------------------------------------------------ helpers
    def _quote(self, token: int):
        for m in self.markets.values():
            if token in m.by_token:
                return m.quote(token)
        return None

    def _flags_path(self) -> str:
        return os.path.join(self.cfg.paths.data_dir, f"day_flags.{self.mode.lower()}.json")

    def _save_day_flags(self) -> None:
        try:
            with open(self._flags_path(), "w", encoding="utf-8") as f:
                json.dump({"day": self.day.isoformat() if self.day else None, "eod_done": self.eod_done,
                           "report_done": self.report_done, "market_seen_today": self.market_seen_today}, f)
        except OSError:
            log.exception("Could not save day flags")

    def _load_day_flags(self) -> None:
        try:
            with open(self._flags_path(), encoding="utf-8") as f:
                d = json.load(f)
            if d.get("day") == dt.datetime.now(IST).date().isoformat():
                self.day = dt.date.fromisoformat(d["day"])
                self.eod_done, self.report_done = bool(d.get("eod_done")), bool(d.get("report_done"))
                self.market_seen_today = bool(d.get("market_seen_today"))
        except (OSError, ValueError, KeyError):
            pass

    # ------------------------------------------------------------------ Telegram callbacks
    def _status_text(self) -> str:
        with self.lock:
            st = self.engine.status()
        p = st["position"]
        pos = (f"open: {p['index']} {p['symbol']} x{p['qty']} @ {p['entry']} ({p['strategy']} {p['direction']}), "
               f"exit score {p['exit_score']} {p['exit_action']}") if p else "no open position"
        b = st["book"]
        ks = " | KILL SWITCH LATCHED" if st["kill_switch"]["latched"] else ""
        return (f"{self.mode} | {pos} | today net {b['day_pnl']:,.0f}, {b['entries_today']} entries | "
                f"week {b['week_pnl']:,.0f}{ks}")

    def _indices_text(self) -> str:
        with self.lock:
            st = self.engine.status()
        lines = [f"{k}: {v['regime'] or '-'} | data {'ok' if v['data_ok'] else 'NOT ok: ' + (v['why'] or '?')} | "
                 f"spot {v['spot'] or '-'} | volume {'yes' if v['future_volume'] else 'NO'} | lots "
                 f"{self.tg.lots_for(k)} | {v['last']}" for k, v in st["indices"].items()]
        return "\n".join(lines) or "No index loaded yet."

    def _risk_text(self) -> str:
        with self.lock:
            st = self.engine.status()
        b, L, ks = st["book"], st["limits"], st["kill_switch"]
        return (f"Limits (x{L['scale']}): premium cap {L['premium_cap']:,}, emergency/trade {L['emergency_per_trade']:,}, "
                f"daily {L['daily']:,}, weekly {L['weekly']:,}, drawdown {L['drawdown']:,}, max entries "
                f"{L['max_entries']}\nToday {b['day_pnl']:,.0f} ({b['entries_today']} entries, "
                f"{b['consecutive_losses']} losses in a row) | week {b['week_pnl']:,.0f} | equity {b['equity']:,.0f} "
                f"(peak {b['peak_equity']:,.0f})\nDay block: {st['day_block'] or 'none'}\nKill switch: "
                f"{'LATCHED - ' + ks['reason'] if ks['latched'] else 'off'}")

    def _signals_text(self) -> str:
        rows = self.store.last_rows("sp_signals", 8)
        if not rows:
            return ""
        return "\n".join(f"{r['ts'][11:16]}Z {r['idx']} {r['strategy']} {r['direction']} {r['score']:.0f} "
                         f"{r['decision']} ({r['regime']})" for r in rows)

    def _kill(self, reason: str) -> str:
        with self.lock:
            return self.engine.kill(reason)

    def _reset_kill(self) -> str:
        with self.lock:
            return self.engine.reset_kill()

    def _request_mode(self, target: str) -> str:
        if target == "LIVE" and not live_allowed(os.environ):
            return ("LIVE is disabled on the VM: LIVE_TRADING_CONFIRM is not YES in .env. Set it there once (the "
                    "master switch), restart, then use /mode live.")
        try:
            write_mode(self._mode_path, target)
        except OSError:
            log.exception("Could not write the mode file")
            return "Could not save the mode (disk?). Nothing changed."
        self._switch_to = target
        when = "now" if self.engine.position is None else "after the open position closes"
        return f"OK: switching to {target} {when}. The bot restarts in {target} mode."

    def _dashboard_status(self) -> dict:
        with self.lock:
            st = self.engine.status()
        st["recent_signals"] = self.store.last_rows("sp_signals", 15)
        st["recent_orders"] = self.store.last_rows("sp_orders", 10, "WHERE mode=?", (self.mode,))
        st["recent_exits"] = self.store.last_rows("sp_exit_assessments", 10)
        return st

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> None:
        self.alerts.send(f"SENSEX PRO started in {self.mode} mode", key="start")
        self.store.app_event("start", {"mode": self.mode})
        self._build_report()
        next_at = time.monotonic()
        while not self.stop.is_set():
            now = dt.datetime.now(IST)
            try:
                self._cycle(now)
            except kex.TokenException:
                self.session_ready = False
                self.alerts.send("Kite token rejected mid-session: complete the login to resume. An open position "
                                 "is NOT being managed until then.", level="CRITICAL", key="token")
            except AuthError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Loop error (continuing)")
                self.alerts.send("Unhandled error in main loop; see logs", level="ERROR", key="loop-error")
            next_at += self.s.loop_interval_s
            lag = next_at - time.monotonic()
            if lag < -5 * self.s.loop_interval_s:
                next_at = time.monotonic()
            self.stop.wait(max(0.0, lag))
        self.alerts.send(f"SENSEX PRO stopping ({self.mode})", key="stop")
        self.store.app_event("stop", {"mode": self.mode, "position_open": self.engine.position is not None})
        if self.engine.position is not None:
            log.warning("Shutting down with an open position: it stays recorded and is recovered on restart")
        self.tg.stop()
        self.client.stop_stream()
        if self.dashboard:
            self.dashboard.stop()
        self.rec.close()
        self.store.close()

    def _cycle(self, now: dt.datetime) -> None:
        ss = self.s.session
        if self._switch_to and self._switch_to != self.mode and self.engine.position is None:
            self.alerts.send(f"Restarting in {self._switch_to} mode now (no open position).", level="WARNING",
                             key="mode-switch")
            self.stop.set()                       # Docker restarts the container; main() reads data/mode.json
            return
        if self.day != now.date():
            self._new_day(now)
        if now.weekday() >= 5 or now.time() < ss.prep_time:
            self.stop.wait(30)
            return
        if now.time() > ss.market_close:
            if not self.eod_done:
                self._end_day(now)
            if not self.report_done and now.time() >= ss.report_time:
                self._end_report(now)
            self.stop.wait(60)
            return
        self._heartbeat(now)
        if not self.session_ready and not self._ensure_session(now):
            return
        live_feed = self.client.market_is_live(self.s.data.spot_stale_s * 6)
        if live_feed:
            self.market_seen_today = True
            self._feed_alerted = False
        elif now.time() >= dt.time(9, 20) and self.market_seen_today and not self._feed_alerted:
            self._feed_alerted = True
            self.alerts.send("Market feed stalled (no SENSEX tick): new entries blocked"
                             + ("; the open position is managed with REST quotes" if self.engine.position else ""),
                             level="CRITICAL", key="feed-stall")
        if not live_feed and self.engine.position is None:
            return                                # pre-open, holiday or stalled feed: nothing to do
        with self.lock:
            self.engine.step(now)

    def _new_day(self, now: dt.datetime) -> None:
        log.info("New day %s", now.date())
        if self.day is not None and self.day != now.date():
            self.eod_done = self.report_done = self.market_seen_today = False
        self.day = now.date()
        self.session_ready = False
        self._auth_alerted = self._feed_alerted = False
        with self.lock:
            self.engine.new_day(now.date())
        self._save_day_flags()

    def _ensure_session(self, now: dt.datetime) -> bool:
        try:
            self.client.authenticate()
        except AuthError as exc:
            if not self._auth_alerted:
                self._auth_alerted = True
                self.alerts.send(f"Not logged in to Kite: {exc}", level="CRITICAL", key="auth")
            self.stop.wait(30)
            return False
        try:
            self.client.load_instruments()           # SENSEX + INDIA VIX tokens; fresh dumps for the day
            self.client.start_stream()
            loaded = []
            for name, m in self.markets.items():
                try:
                    m.load(now.date())
                    loaded.append(name)
                except Exception:  # noqa: BLE001
                    m.ready = False
                    log.exception("Index %s unavailable this session", name)
            pos = self.engine.position or self.store.open_position(self.mode)
            if pos is not None:
                self.client.subscribe_full({pos.contract.token: pos.contract.key})
            self.capital = self.client.available_cash() if self.live else self.cfg.paper_capital
        except kex.TokenException:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Session start failed; retrying")
            self.alerts.send("Session start failed; retrying shortly (see logs)", level="ERROR", key="session-fail")
            self.stop.wait(10)
            return False
        with self.lock:
            self.engine.start_session(self.capital, now.date())
            if self.engine.position is not None:
                self.client.subscribe_full({self.engine.position.contract.token: self.engine.position.contract.key})
            L = self.engine.limits
        self.session_ready = True
        what = (f"capital {self.capital:,.0f}; indices {', '.join(loaded)}; premium cap {L.max_premium_per_entry:,.0f}, "
                f"emergency {L.emergency_loss_per_trade:,.0f}, daily {L.daily_loss_limit:,.0f}")
        self.notifier.status_row("SESSION_READY", what)
        self.alerts.send(f"Session ready ({self.mode}): {what}", key="session")
        return True

    def _end_day(self, now: dt.datetime) -> None:
        with self.lock:
            self.engine.end_of_session(now)
            b = self.engine.book
        self.rec.event(now.isoformat(timespec="seconds"), "day_summary", "",
                       {"realized": b.day_realized, "trades": b.entries_today, "mode": self.mode,
                        "market_open": self.market_seen_today})
        self.alerts.send(f"Day done ({self.mode}): net P&L {b.day_realized:,.0f} over {b.entries_today} entries")
        self.notifier.status_row("DAY_DONE", f"net {b.day_realized:,.0f} over {b.entries_today} entries")
        self.client.stop_stream()
        self.session_ready, self.eod_done = False, True
        try:
            self.store.backup(os.path.join(self.cfg.paths.data_dir, "sxbot-backup.db"))   # daily consistent copy
        except Exception:  # noqa: BLE001
            log.exception("Database backup failed")
        self._save_day_flags()

    def _build_report(self) -> tuple[bool, dict]:
        return self.report.build(self.rec, self.rec.day_summary_days(self.report.since, self.mode))

    def _end_report(self, now: dt.datetime) -> None:
        try:
            if self.market_seen_today:
                try:
                    self.client.authenticate()
                except Exception:  # noqa: BLE001
                    log.exception("Kite authentication for the report failed: trying the saved session")
                self.report.save_live(self.client, now.date())
            changed, summary = self._build_report()
            if changed and self.report.url:
                self.alerts.send(self.report.summary_text(now.date().isoformat(), summary), key="report")
        except Exception:  # noqa: BLE001
            log.exception("Report page update failed")
        self.report_done = True
        self._save_day_flags()

    def _heartbeat(self, now: dt.datetime, every_s: float = 900.0) -> None:
        if not self.sheets.enabled or time.monotonic() - self._last_heartbeat < every_s:
            return
        self._last_heartbeat = time.monotonic()
        try:
            with self.lock:
                st = self.engine.status()
            self.sheets.append("Health", HEALTH_HEADER, [health_row(now, self.mode, health_collect(self.cfg, now, st))])
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat failed")


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": dt.datetime.fromtimestamp(r.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
             "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    txt = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=5)
    txt.setFormatter(fmt)
    js = RotatingFileHandler(os.path.join(log_dir, "sensexpro.jsonl"), maxBytes=10_000_000, backupCount=5)
    js.setFormatter(JsonFormatter())
    for h in (out, txt, js):
        root.addHandler(h)


def main() -> int:
    ap = argparse.ArgumentParser(description="SENSEX PRO (Kite Connect)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--paper", action="store_true", help="paper trading (no orders sent)")
    g.add_argument("--live", action="store_true", help="real orders (needs LIVE_TRADING_CONFIRM=YES)")
    g.add_argument("--auto", action="store_true",
                   help="mode from data/mode.json (Telegram /mode); PAPER unless it says LIVE and LIVE_TRADING_CONFIRM=YES")
    args = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg.log_dir)
    try:
        s = load_settings()
    except ConfigError as exc:
        log.critical("Invalid configuration, refusing to start: %s", exc)
        return 2
    if args.live and os.getenv("LIVE_TRADING_CONFIRM") != "YES":
        log.error("Refusing --live: set LIVE_TRADING_CONFIRM=YES to acknowledge real-money risk")
        return 2
    live = args.live
    if args.auto:
        mode, note = startup_mode(os.path.join(cfg.paths.data_dir, "mode.json"), os.environ)
        if note:
            log.error(note)
        live = mode == "LIVE"
    bot = Bot(cfg, s, live=live)
    for sg in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sg, lambda *_: bot.stop.set())
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
