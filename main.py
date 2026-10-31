"""Entry point: python main.py --paper | --live

Runs as a long-lived daemon (restart-on-failure under Docker/systemd). Each
trading day it: authenticates with the token written by token_service.py,
loads instruments, streams data, records the full market state, forward-tests
every strategy variant in the shadow book, and trades only the variants listed
in the approvals file for the current mode.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

from kiteconnect import exceptions as kex

from alerts import Alerter
from config import IST, Config, capital_tier, load_config
from costs import trade_cost
from entry_filter import EntryParams, evaluate_entry
from exit_engine import ExitEngine, ExitParams
from explorer import pick_explore
from execution import (LiveBroker, OrderLeg, PaperBroker, apply_exit_fills, round_tick as _round_tick,  # noqa: F401
                       validate_margin, validate_plan_quotes)
from market_context import MarketContext
from market_data import MarketData, Snapshot
from pb_indicators import Candle
from playbook import (DayState, Playbook, PlaybookParams, TrendState, build_condor, build_credit, build_single,
                      is_playbook, is_single, trend_state)
from report_site import ReportSite
from telegram_control import TelegramControl
from recorder import Recorder
from health_report import HEALTH_HEADER, collect as health_collect, sheet_row as health_row
from risk_manager import ManagedPosition, OpenLeg, RiskManager
from sheets_sink import ORDERS_HEADER, STATUS_HEADER, TRADES_HEADER, SheetsSink
from shadow import ShadowBook
from sheet_log import SheetLogHandler
from strategy_engine import Leg, RegimeDetector, StrategyEngine, TradePlan
from strategy_library import _round_trip_cost, build_library, build_plan, load_approved
from zerodha_client import AuthError, OptionContract, ZerodhaClient

log = logging.getLogger("bot")


# --------------------------------------------------------- state persistence
def _contract_to_dict(c: OptionContract) -> dict:
    d = dataclasses.asdict(c)
    d["expiry"] = c.expiry.isoformat()
    return d


def _contract_from_dict(d: dict) -> OptionContract:
    d = dict(d)
    d["expiry"] = dt.date.fromisoformat(d["expiry"])
    return OptionContract(**d)


def _position_to_dict(pos: ManagedPosition) -> dict:
    p = pos.plan
    return {
        "plan": {"name": p.name, "regime": p.regime, "expiry": p.expiry.isoformat(),
                 "legs": [{"contract": _contract_to_dict(l.contract), "side": l.side, "ref_price": l.ref_price,
                           "ratio": l.ratio} for l in p.legs],
                 "lot_size": p.lot_size, "max_loss_unit": p.max_loss_unit,
                 "max_profit_unit": p.max_profit_unit, "rationale": p.rationale,
                 "variant_id": p.variant_id, "sl_frac": p.sl_frac, "tp_frac": p.tp_frac,
                 "spot_lo": p.spot_lo, "spot_hi": p.spot_hi, "spot_be": p.spot_be, "entry_spot": p.entry_spot},
        "lots": pos.lots,
        "legs": [{"contract": _contract_to_dict(l.contract), "side": l.side, "qty": l.qty,
                  "entry_price": l.entry_price} for l in pos.legs],
        "entered_at": pos.entered_at.isoformat(), "max_loss": pos.max_loss,
        "max_profit": pos.max_profit, "peak_pnl": pos.peak_pnl, "exit_state": pos.exit_state,
    }


def _position_from_dict(d: dict) -> ManagedPosition:
    p = d["plan"]
    plan = TradePlan(
        name=p["name"], regime=p["regime"], expiry=dt.date.fromisoformat(p["expiry"]),
        legs=tuple(Leg(_contract_from_dict(l["contract"]), l["side"], l["ref_price"], l.get("ratio", 1))
                   for l in p["legs"]),
        lot_size=p["lot_size"], max_loss_unit=p["max_loss_unit"], max_profit_unit=p["max_profit_unit"],
        rationale=p["rationale"], variant_id=p.get("variant_id", ""),
        sl_frac=p.get("sl_frac"), tp_frac=p.get("tp_frac"), spot_lo=p.get("spot_lo"), spot_hi=p.get("spot_hi"),
        spot_be=p.get("spot_be"), entry_spot=p.get("entry_spot"))
    legs = [OpenLeg(_contract_from_dict(l["contract"]), l["side"], l["qty"], l["entry_price"]) for l in d["legs"]]
    return ManagedPosition(plan, d["lots"], legs, dt.datetime.fromisoformat(d["entered_at"]),
                           d["max_loss"], d["max_profit"], d.get("peak_pnl", 0.0), d.get("exit_state") or {})


# ---------------------------------------------------------------------- bot
class Bot:
    def __init__(self, cfg: Config, live: bool) -> None:
        self.cfg, self.live = cfg, live
        self.mode = "LIVE" if live else "PAPER"
        self.client = ZerodhaClient(cfg.credentials, cfg.paths.token_file)
        self.engine = StrategyEngine(self.client, cfg.strategy)
        self.detector = RegimeDetector(cfg.strategy)
        self.md = MarketData(self.client, self.engine, self.detector, cfg.strategy,
                             cfg.paths.daily_cache, cfg.chain_interval_s)
        self.risk = RiskManager(cfg.risk, cfg.schedule)
        self.broker = LiveBroker(self.client, cfg) if live else PaperBroker(cfg.execution.paper_slippage_pct,
                                                                             quote_fn=self._paper_quote)
        self.alerts = Alerter(cfg.alerts.telegram_token, cfg.alerts.telegram_chat_id, background=True)
        self.rec = Recorder(cfg.paths.db)
        self.sheets = SheetsSink(cfg.sheets_url, cfg.sheets_secret)
        self.sheet_log = SheetLogHandler(self.sheets, self.mode)     # mirrors key log lines to the sheet's Log tab
        logging.getLogger().addHandler(self.sheet_log)
        if self.sheets.enabled:
            self.sheet_log.start()
        self.specs = build_library()
        self.spec_by_id = {s.id: s for s in self.specs}
        self.xp = ExitParams.from_env()
        self.exit_engine = ExitEngine(self.xp)
        # the shadow book exits with the same engine as the bot, so its forward evidence matches what would be traded
        self.shadow = ShadowBook(self.specs, self.rec, cfg.risk, cfg.schedule, cfg.costs,
                                 exit_engine=ExitEngine(self.xp, verbose=False) if self.xp.enabled else None)
        self.approved: dict[str, float] = {}
        self.stop = threading.Event()
        self.position: Optional[ManagedPosition] = None
        self.trades_today = 0
        self.realized = 0.0
        self.capital = 0.0
        self.day: Optional[dt.date] = None
        self.session_ready = False
        self.eod_done = False
        self._auth_alerted = False
        self._halt_alerted = False
        self._exit_failures = 0
        self._entry_failures = 0
        self._entry_block_until = 0.0
        self._stall_alerted = False
        self._last_px: dict[int, float] = {}
        self._last_state_save = 0.0
        self._last_heartbeat = 0.0
        self._last_flow = 0.0
        self._last_chain_rec = 0.0
        self.engine.use_stream = cfg.stream_chain
        self.stream_full = os.getenv("STREAM_FULL", "1") != "0"   # tick-level OI/volume/depth near ATM
        self._approved_stamp: Optional[tuple] = None
        self.ep = EntryParams.from_env()
        self.ctx = MarketContext()
        self._last_snap: Optional[Snapshot] = None
        self._last_snap_at = -1e9                    # monotonic time of _last_snap (paper fills use its bid/ask)
        self._explore_block: set[tuple[str, str]] = set()
        self._gate_logged: dict[str, float] = {}
        self._last_exit_status = 0.0
        self._margin_cache: dict[tuple, tuple[float, Optional[float]]] = {}
        # Playbook (playbook.py): trend single-leg / range multi-leg, no stop-loss or trailing stop. PLAYBOOK=0 restores
        # the approvals + explorer entry path.
        self.pb_enabled = os.getenv("PLAYBOOK", "1") != "0"
        self.pb = Playbook(PlaybookParams.from_env())
        self._pb_cache: tuple[Optional[tuple], Optional[TrendState]] = (None, None)
        self._pb_ready_cache: tuple[Optional[str], float, bool] = (None, -1e18, False)
        # Trading report page (report_site.py): ONE Cloud Storage object gs://REPORT_GCS_BUCKET/r/<REPORT_TOKEN>/index.html,
        # overwritten in place on every rebuild, so the link never changes and works while the VM is off.
        tok, bucket = os.getenv("REPORT_TOKEN", "").strip(), os.getenv("REPORT_GCS_BUCKET", "").strip()
        self.report = ReportSite(cfg.paths.data_dir, os.getenv("REPORT_SINCE", "2026-11-02"), cfg.costs,
                                 os.getenv("REPORT_BACKTEST_URL", "https://claude.ai/artifact/X3zZSYuYHr3B6Lwqt9jhti"),
                                 bucket, f"r/{tok}/index.html" if bucket and tok else "")
        self.tg = TelegramControl(cfg.alerts.telegram_token, cfg.alerts.telegram_chat_id,
                                  os.path.join(cfg.paths.data_dir, "settings.json"),
                                  reply=lambda t: self.alerts.send(t), status_fn=self._status_text,
                                  default_single=int(os.getenv("SINGLE_LOTS", "2")),
                                  default_multi=int(os.getenv("MULTI_LOTS", "2")),
                                  report_url=self.report.url)
        self._load_state()
        self.tg.start()

    # ------------------------------------------------------------ persistence
    def _state_path(self) -> str:
        """One state file per mode, so a paper restart can never overwrite a live position's state."""
        base = self.cfg.paths.state
        if not self.live:
            return base
        root, ext = os.path.splitext(base)
        return f"{root}.live{ext}"

    def _save_state(self) -> None:
        doc = {"date": (self.day or dt.date.today()).isoformat(), "mode": self.mode,
               "realized": self.realized, "trades_today": self.trades_today, "eod_done": self.eod_done,
               "risk": self.risk.export(),
               "playbook": self.pb.day.export() if hasattr(self, "pb") else {},
               "position": _position_to_dict(self.position) if self.position else None}
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, path)
        except OSError:              # e.g. disk full: the in-memory position is still managed; do not abort the trade flow
            log.critical("Could not write the state file: a restart now would lose track of the position", exc_info=True)
            self.alerts.send("State file write FAILED (disk full?). Free space on the VM.", level="CRITICAL",
                             key="state-write")
        self._last_state_save = time.monotonic()

    def _load_state(self) -> None:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        if doc.get("mode") != self.mode:
            log.warning("Ignoring state file written in %s mode", doc.get("mode"))
            return
        try:
            self.day = dt.date.fromisoformat(doc["date"])
            self.realized = float(doc.get("realized", 0.0))
            self.trades_today = int(doc.get("trades_today", 0))
            self.eod_done = bool(doc.get("eod_done", False))   # a restart after the close must not repeat the day summary
            self.risk.restore(doc.get("risk", {}))
            if hasattr(self, "pb"):
                self.pb.day = DayState.restore(doc.get("playbook") or {})
            if doc.get("position"):
                self.position = _position_from_dict(doc["position"])
                log.warning("Restored open position %s from state file", self.position.plan.name)
        except (KeyError, ValueError, TypeError):
            log.exception("State file unreadable; starting clean")
            self.position = None

    # -------------------------------------------------------------- lifecycle
    def _build_report(self) -> tuple[bool, dict]:
        return self.report.build(self.rec, self.rec.day_summary_days(self.report.since, self.mode))

    def run(self) -> None:
        self.alerts.send(f"Bot started in {self.mode} mode", key="start")
        self._build_report()                       # the report link works right after a (re)start
        next_at = time.monotonic()
        while not self.stop.is_set():
            now = dt.datetime.now(IST)
            try:
                self._cycle(now)
            except kex.TokenException:
                self.session_ready = False
                self.alerts.send("Kite token rejected mid-session: complete the login to resume. "
                                 "Open positions are NOT being managed until then.",
                                 level="CRITICAL", key="token")
            except AuthError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Loop error (continuing)")
                self.alerts.send("Unhandled error in main loop; see logs", level="ERROR", key="loop-error")
            next_at += self.cfg.loop_interval_s                    # fixed-rate: no drift from processing time
            lag = next_at - time.monotonic()
            if lag < -5 * self.cfg.loop_interval_s:                 # fell far behind: resync, don't burst
                next_at = time.monotonic()
            self.stop.wait(max(0.0, lag))
        if self.position:
            log.warning("Shutting down with open position; flattening")
            try:
                self._exit("SHUTDOWN")
            except Exception:  # noqa: BLE001
                log.critical("Could not flatten on shutdown: check positions manually", exc_info=True)
        self.tg.stop()
        self.client.stop_stream()
        self.rec.close()

    def _cycle(self, now: dt.datetime) -> None:
        s = self.cfg.schedule
        if self.day != now.date():
            self._new_day(now)
        if now.weekday() >= 5 or now.time() < s.prep_time:
            self.stop.wait(30)
            return
        if now.time() > s.market_close:
            if not self.eod_done:
                self._end_day(now)
            self.stop.wait(60)
            return
        self._refresh_approvals(now)
        self._heartbeat(now)
        if not self.session_ready and not self._ensure_session(now):
            return
        if not self.client.market_is_live():
            if self.position and now.time() >= dt.time(9, 20):
                if not self._stall_alerted:           # never leave an open position unmanaged because the feed looks stalled
                    self._stall_alerted = True
                    self.alerts.send("Market feed looks stalled while a position is open: managing it with REST prices.",
                                     level="CRITICAL", key="feed-stall")
                self._manage(now)
            return                                    # pre-open, holiday or stalled feed
        self._stall_alerted = False
        snap: Optional[Snapshot] = None
        try:
            snap = self.md.snapshot(now)
        except (kex.TokenException, AuthError):
            raise
        except Exception:  # noqa: BLE001 - a data problem must not stop an open position being managed
            log.exception("Snapshot failed")
            self.alerts.send("Market snapshot failing; see logs", level="ERROR", key="snapshot-error")
        if snap is not None:
            try:
                if snap.fresh:
                    full = time.monotonic() - self._last_chain_rec >= self.cfg.chain_record_every_s
                    if full:
                        self._last_chain_rec = time.monotonic()
                    self.ctx.update(snap, now)
                    self._last_snap, self._last_snap_at = snap, time.monotonic()
                    self.rec.record_snapshot(snap, with_chain=full)
                    self.shadow.step(snap, now, self.ctx.signals())
                    self._record_flow(now, snap)
                else:
                    self.shadow.flatten_if_due(now)   # stale feed must not leave virtual positions open past square-off
            except (kex.TokenException, AuthError):
                raise
            except Exception:  # noqa: BLE001 - recording / shadow bugs must never skip position management
                log.exception("Recording / shadow step failed")
        if self.position:
            self._manage(now)
        elif not self.risk.halted and snap is not None:
            self._try_entry(now, snap)
        if self.risk.halted and not self._halt_alerted:
            self._halt_alerted = True
            self.alerts.send(f"Trading halted: {self.risk.halt_reason}", level="CRITICAL")

    def _record_flow(self, now: dt.datetime, snap: Snapshot) -> None:
        """Store ltp / OI / volume / bid / ask for ATM +/- flow_strikes on every fresh chain (1 s by default).
        Purely additive data capture: any failure is logged and ignored."""
        cfg = self.cfg
        if not self.stream_full or snap.chain is None \
                or time.monotonic() - self._last_flow < cfg.flow_record_every_s - 0.05:
            return
        self._last_flow = time.monotonic()
        try:
            ch = snap.chain
            ts = now.isoformat(timespec="seconds")
            rows = [(ts, q.contract.symbol, k, kind, q.ltp, q.oi, q.volume, q.bid, q.ask)
                    for (k, kind), q in ch.quotes.items() if abs(k - ch.atm) <= cfg.flow_strikes * ch.step]
            self.rec.flow(rows)
        except Exception:  # noqa: BLE001
            log.exception("Flow capture failed (continuing without it)")

    def _heartbeat(self, now: dt.datetime, every_s: float = 900.0) -> None:
        """Append a health summary row to the Google Sheet ('Health' tab) every 15 minutes. Never raises."""
        if not self.sheets.enabled or time.monotonic() - self._last_heartbeat < every_s:
            return
        self._last_heartbeat = time.monotonic()
        try:
            self.sheets.append("Health", HEALTH_HEADER, [health_row(now, self.mode, health_collect(self.cfg, now))])
        except Exception:  # noqa: BLE001 - monitoring must never disturb trading
            log.exception("Heartbeat failed")

    def _refresh_approvals(self, now: dt.datetime) -> None:
        """Load approved variants at startup (also after a same-day restart, when the new-day step is
        skipped) and again whenever the approvals file changes or the date rolls over."""
        paths = [self.cfg.paths.approved_live] if self.live else \
            [self.cfg.paths.approved_backtest, self.cfg.paths.approved_intraday,
             self.cfg.paths.approved_live]                                       # paper also runs promoted variants
        mtimes = []
        for p in paths:
            try:
                mtimes.append(os.stat(p).st_mtime)
            except OSError:
                mtimes.append(None)
        stamp = (tuple(mtimes), now.date())
        if stamp == self._approved_stamp:
            return
        self._approved_stamp = stamp
        approved: dict[str, float] = {}
        for p in paths:
            approved.update(load_approved(p, now.date(), capital_tier(getattr(self, "capital", 0.0) or self.cfg.paper_capital)))
        self.approved = approved
        if getattr(self, "pb_enabled", False):        # the playbook trades without approvals: no misleading alert
            log.info("%d approved variants loaded (not used for entries: the playbook is on)", len(self.approved))
        elif self.approved:
            log.info("%d approved variants loaded from %s", len(self.approved), ", ".join(paths))
            self.alerts.send(f"{len(self.approved)} approved variants loaded", key="approved-loaded")
        else:
            log.warning("No valid approved variants in %s: shadow data collection only, no trades", paths)
            self.alerts.send("No approved strategies: only shadow data collection is running", key="noapprove")

    def _new_day(self, now: dt.datetime) -> None:
        log.info("New trading day %s", now.date())
        self.day = now.date()
        self.realized, self.trades_today = 0.0, 0
        self.risk.reset_day()
        self.shadow.reset_day()
        self.md.reset_day()
        self.ctx.reset()
        self._last_snap = None
        try:
            n = self.rec.prune_flow(self.cfg.flow_retention_days, now.date().isoformat())
            if n:
                log.info("Pruned %d old flow rows", n)
        except Exception:  # noqa: BLE001
            log.exception("Prune failed")
        self.session_ready = self.eod_done = False
        self._auth_alerted = self._halt_alerted = False
        self._exit_failures = self._entry_failures = 0
        self._entry_block_until, self._stall_alerted = 0.0, False
        if self.position and not self.live:    # a paper position left from an earlier day refers to expired contracts
            log.warning("Dropping stale paper position %s from %s", self.position.plan.variant_id,
                        self.position.entered_at.date())
            self.position = None
        self._approved_stamp = None            # force a reload below
        self._refresh_approvals(now)
        self._refresh_explore_block(now)
        self._save_state()

    def _refresh_explore_block(self, now: dt.datetime) -> None:
        """Strategy families (template + trend filter) whose recent shadow results are clearly negative are not
        paper-explored. Uses only trades closed BEFORE today, so it is walk-forward. Never raises."""
        c = self.cfg
        if c.explore_fwd_min_trades <= 0:
            self._explore_block = set()
            return
        try:
            since = (now.date() - dt.timedelta(days=c.explore_fwd_days)).isoformat()
            rows = self.rec.shadow_results(since, now.date().isoformat())
        except Exception:  # noqa: BLE001 - evidence is advisory; never block the day start
            log.exception("Could not read shadow results for the exploration filter")
            return
        fam: dict[tuple[str, str], list[float]] = {}
        for vid, net in rows:
            spec = self.spec_by_id.get(vid)
            if spec is not None:
                fam.setdefault((spec.template, spec.trend), []).append(net)
        block = set()
        for key, nets in fam.items():
            n = len(nets)
            if n < c.explore_fwd_min_trades:
                continue
            mean = sum(nets) / n
            var = sum((x - mean) ** 2 for x in nets) / (n - 1)
            t = mean / (var / n) ** 0.5 if var > 0 else (-99.0 if mean < 0 else 99.0)
            if t <= c.explore_fwd_max_t:
                block.add(key)
        self._explore_block = block
        if block:
            log.info("Exploration skips %d strategy families with negative forward evidence: %s", len(block),
                     ", ".join(f"{t}/{m}" for t, m in sorted(block)))

    def _paper_quote(self, c: OptionContract) -> Optional[tuple[float, float]]:
        """(bid, ask) of this contract from the latest chain (at most 10 s old), so paper fills pay the real spread."""
        snap = self._last_snap
        if snap is None or snap.chain is None or time.monotonic() - self._last_snap_at > 10.0:
            return None
        q = snap.chain.get(c.strike, c.kind)
        if q is None or q.contract.symbol != c.symbol or not (q.bid and q.ask and q.ask >= q.bid > 0):
            return None
        return q.bid, q.ask

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
            self.client.load_instruments()
            self.client.start_stream()               # replaces any earlier stream (no leaked sockets on retries)
            if self.position:
                self.client.subscribe_tokens({l.contract.token: l.contract.key for l in self.position.legs})
            self.capital = self.client.available_cash() if self.live else self.cfg.paper_capital
        except kex.TokenException:
            raise
        except Exception:  # noqa: BLE001 - network blip while starting the session: back off and retry
            log.exception("Session start failed; retrying")
            self.alerts.send("Session start failed; retrying shortly (see logs)", level="ERROR", key="session-fail")
            self.stop.wait(10)
            return False
        self.risk.apply_capital(self.capital, self.cfg.capital_profile)
        log.info("Session ready: mode=%s capital=%.0f tier=%s", self.mode, self.capital, capital_tier(self.capital))
        self._approved_stamp = None            # approvals are per account-size tier: reload now capital is known
        self._refresh_approvals(now)
        if self.live:
            self._reconcile()
        self.session_ready = True
        what = (f"playbook ON, lots single {self.tg.single_lots} / multi {self.tg.multi_lots}"
                if getattr(self, "pb_enabled", False) else f"{len(self.approved)} approved variants")
        self.sheets.append("Status", STATUS_HEADER, [[now.isoformat(timespec="seconds"), self.mode,
                                                      "SESSION_READY", f"capital {self.capital:,.0f}, {what}"]])
        self.alerts.send(f"Session ready ({self.mode}), capital {self.capital:,.0f}, {what}", key="session")
        return True

    def _reconcile(self) -> None:
        """Compare the bot's view of its position with the broker's net positions."""
        broker = self.client.net_positions()
        expected: dict[str, int] = {}
        if self.position:
            for l in self.position.legs:
                expected[l.contract.symbol] = expected.get(l.contract.symbol, 0) + \
                    (l.qty if l.side == "BUY" else -l.qty)
        if broker == expected:
            return
        if self.position and not broker:
            self.alerts.send("State shows an open position but the broker is flat: assuming it was "
                             "closed outside the bot; clearing state.", level="WARNING")
            self.position = None
            self._save_state()
        elif not self.cfg.allow_foreign_positions:
            self.risk.halt("POSITION_MISMATCH")
            self.alerts.send(f"Position mismatch. bot={expected} broker={broker}. Trading halted: "
                             "reconcile manually, then restart.", level="CRITICAL")

    def _end_day(self, now: dt.datetime) -> None:
        if self.position and self.cfg.execution.product == "MIS":
            self.alerts.send("MIS position still open after close: verify broker auto square-off",
                             level="CRITICAL")
        self.shadow.reset_day()
        market_open = self._last_snap is not None           # a live snapshot was seen today (False on holidays)
        self.rec.event(now.isoformat(timespec="seconds"), "day_summary", "",
                       {"realized": self.realized, "trades": self.trades_today, "mode": self.mode,
                        "market_open": market_open})
        self.alerts.send(f"Day done ({self.mode}): net PnL {self.realized:,.0f} over {self.trades_today} trades")
        try:                                                # report page: never allowed to disturb the day's close
            if self.session_ready and market_open:
                self.report.save_live(self.client, now.date())   # the account's real fills (read-only)
            changed, summary = self._build_report()
            if changed and self.report.url:
                self.alerts.send(self.report.summary_text(now.date().isoformat(), summary), key="report")
        except Exception:  # noqa: BLE001
            log.exception("Report page update failed")
        self.sheets.append("Status", STATUS_HEADER, [[now.isoformat(timespec="seconds"), self.mode, "DAY_DONE",
                                                      f"net {self.realized:,.0f} over {self.trades_today} trades"]])
        self.client.stop_stream()
        self.session_ready, self.eod_done = False, True
        self._save_state()

    # ---------------------------------------------------------------- trading
    def _ltps(self, pos: ManagedPosition, max_age_s: float = 5.0) -> dict[int, float]:
        """Leg prices for stops and exits. A leg with no valid price right now (no trades, API blip) uses its last good
        price, else its entry price, so one dead quote can never block a stop or square-off. A streamed tick older than
        `max_age_s` is re-fetched over REST."""
        out: dict[int, float] = {}
        for l in pos.legs:
            tok = l.contract.token
            try:
                px = self.client.get_ltp(tok, max_age_s=max_age_s)
                self._last_px[tok] = px
            except kex.TokenException:
                raise
            except Exception:  # noqa: BLE001
                px = self._last_px.get(tok) or l.entry_price
                log.warning("No valid price for %s: using %.2f", l.contract.symbol, px)
            out[tok] = px
        return out

    # -------------------------------------------------------------- playbook
    def _pb_trend(self, now: dt.datetime) -> Optional[TrendState]:
        """15-minute trend state from Kite's 5-minute history. Only candles that had CLOSED when the history was fetched
        are used: the last candle of a fetch is still forming, and stays a stale part-bar until the next refresh (a
        few seconds after each 5-minute close), so it must never be read as a completed bar. Recomputed per fetch."""
        hist, fetched = self.md.completed_5m()
        if not hist or fetched is None:
            self._pb_cache = (None, None)
            return None
        key = (fetched, len(hist))
        if key == self._pb_cache[0]:
            return self._pb_cache[1]
        ts: Optional[TrendState] = None
        try:
            bars = [Candle(x["date"], float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]))
                    for x in hist]
            ts = trend_state(bars, min(now, fetched))
        except Exception:  # noqa: BLE001 - no signal is safer than a crash
            log.exception("Playbook trend state failed")
        self._pb_cache = (key, ts)
        return ts

    def _try_playbook(self, now: dt.datetime, snap: Snapshot) -> None:
        if not snap.fresh or snap.chain is None or self.risk.halted or time.monotonic() < self._entry_block_until:
            return
        if now.time() >= self.cfg.schedule.square_off:
            return
        ts = self._pb_trend(now)
        d = self.pb.single_signal(ts, now, snap.vix)
        if d.action == "single":                       # a strong trend: single leg first
            plan, lots, why_not = self._pb_single_ready(now, snap, d)
            if plan is None and why_not.startswith("quote") and ts is not None \
                    and (now - ts.bar_end).total_seconds() < self.PB_QUOTE_RETRY_S:
                return                                 # quotes still settling: retry next second, the signal stays live
            if plan is not None:
                self._enter(now, snap, plan, lots, 100.0, 1)
            if self.position:
                self.pb.took_single(d)
            else:
                self.pb.skipped_signal(d)
                self.alerts.send(f"Trend signal NOT traded ({d.why}): {why_not or 'entry failed'}",
                                 level="WARNING", key="pb-single-skip")
            self._save_state()
            return
        d = self.pb.range_signal(ts, now, snap.vix)
        if d.action in ("condor", "credit"):
            plan = (build_condor(snap.chain, d.short_pct, d.wing_pct, d.why) if d.action == "condor"
                    else build_credit(snap.chain, d.direction, d.short_pct, d.wing_pct, d.why))
            if plan is None or not validate_plan_quotes(plan, snap.chain).ok:
                return                                 # quotes not ready: try again next second
            lots = self._pb_lots(plan, self.tg.multi_lots)
            if lots:
                self._enter(now, snap, plan, lots, 50.0, 1)
            self.pb.took_range()                       # one attempt per day (a margin refusal is not retried)
            if not self.position:
                self.alerts.send(f"{plan.name} NOT traded ({d.why}): not even 1 lot fits the margin",
                                 level="WARNING", key="pb-multi-skip")
            self._save_state()

    PB_QUOTE_RETRY_S = 60.0          # a trend signal whose option is not quoted yet is retried this long
    PB_TICK_MAX_AGE_S = 60.0         # playbook legs: streamed ticks this old are still the price (see _manage_playbook)
    PB_READY_RECHECK_S = 10.0        # pre-emption tradability re-check interval

    def _pb_single_ready(self, now: dt.datetime, snap: Snapshot, d) -> tuple[Optional[TradePlan], int, str]:
        """(plan, lots, '') when the single-leg trade for signal `d` can be placed now, else (None, 0, why not).
        A reason starting with 'quote' is transient (the stream is still filling the strike)."""
        if snap.chain is None:
            return None, 0, "quote: no option chain yet"
        plan = build_single(snap.chain, d.direction, now, d.why)
        if plan is None:
            return None, 0, "quote: strike not quoted"
        qc = validate_plan_quotes(plan, snap.chain)
        if not qc.ok:
            return None, 0, f"quote: {qc.reason}"
        lots = self._pb_lots(plan, self.tg.single_lots)
        if lots < 1:
            return None, 0, "not even 1 lot fits the margin (or the margin query failed)"
        return plan, lots, ""

    def _pb_lots(self, plan: TradePlan, wanted: int) -> int:
        """The Telegram lot setting, or fewer (down to 1) when that many lots need more margin than the capital allows
        (Kite's basket margin, the same check _enter makes). 0 = not even one lot fits, or the margin query failed."""
        fn = self._margin_fn(plan)
        cash = self.client.available_cash() if self.live else self.capital
        limit = cash * self.risk.p.margin_utilisation
        for n in range(max(wanted, 1), 0, -1):
            m = fn(n)
            if m is None:
                return 0
            if m <= limit:
                if n < wanted:
                    log.info("PLAYBOOK %s: %d lot(s) need more margin than allowed; trading %d", plan.variant_id,
                             wanted, n)
                return n
        return 0

    def _manage_playbook(self, now: dt.datetime, pos: ManagedPosition) -> None:
        """No stop-loss and no trailing stop: exit on trend reversal, the Rs loss cap, a trend signal while a condor is
        open, or square-off. Only loss-control / broker-safety halts (FLATTEN_HALTS) can close it otherwise."""
        # Kite ticks a contract only when it trades or its book changes, so far-OTM wings are often quiet for longer than
        # 5 s; their last tick IS their price. A short REST fallback window would fire ~1 quote call per leg per second
        # (Kite allows ~1/s in total) and stall this loop, so playbook legs accept ticks up to PB_TICK_MAX_AGE_S old.
        pnl = pos.pnl(self._ltps(pos, max_age_s=self.PB_TICK_MAX_AGE_S))
        pos.peak_pnl = max(pos.peak_pnl, pnl)
        reason: Optional[str] = None
        if self.risk.halted:
            reason = self.risk.update_portfolio(self.capital, self.realized + pnl)   # returns only FLATTEN halts
        if not reason:
            ts = self._pb_trend(now)
            pending = False
            snap = self._last_snap
            if not is_single(pos.plan) and snap is not None:
                d = self.pb.single_signal(ts, now, snap.vix)
                # close the condor / credit spread only if the trend trade can really be placed right after; checked at
                # most every PB_READY_RECHECK_S per signal bar (it may query Kite margins and cash)
                if d.action == "single":
                    key, at, ok = self._pb_ready_cache
                    if key != d.bar_key or time.monotonic() - at >= self.PB_READY_RECHECK_S:
                        ok = self._pb_single_ready(now, snap, d)[0] is not None
                        self._pb_ready_cache = (d.bar_key, time.monotonic(), ok)
                    pending = ok
            reason = self.pb.exit_reason(pos.plan, pnl, ts, pending)
        if not reason and now.time() >= self.cfg.schedule.square_off and (
                self.cfg.execution.product == "MIS" or pos.plan.expiry <= now.date()):
            reason = "SQUARE_OFF_TIME"
        if time.monotonic() - self._last_exit_status > 60:
            self._last_exit_status = time.monotonic()
            log.info("MTM %s pnl=%.0f peak=%.0f (no stop/trail; cap %.0f)", pos.plan.variant_id, pnl, pos.peak_pnl,
                     self.pb.p.loss_cap_rs)
        if time.monotonic() - self._last_state_save > 30:
            self._save_state()
        if reason:
            self._exit(reason)

    def _status_text(self) -> str:
        pos = self.position
        p = (f"open: {pos.plan.name} x{pos.lots} [{pos.plan.variant_id}] since {pos.entered_at:%H:%M}"
             if pos else "no open position")
        return (f"{self.mode} | {p} | today net {self.realized:,.0f} over {self.trades_today} trades | "
                f"lots single {self.tg.single_lots}, multi {self.tg.multi_lots} | playbook "
                f"{'ON' if self.pb_enabled else 'OFF'}")

    def _manage(self, now: dt.datetime) -> None:
        pos = self.position
        assert pos
        if is_playbook(pos.plan):
            self._manage_playbook(now, pos)
            return
        pnl = pos.pnl(self._ltps(pos))
        if self.xp.enabled:
            # Safety kill-switches first (daily loss / halts). The day-level trailing stop is off: it could exit
            # inside the neutral zone. Profit protection is the exit engine's laddered trail on the combined P&L.
            reason = self.risk.update_portfolio(self.capital, self.realized + pnl,
                                                day_trail=not self.xp.suppress_day_trail)
            if not reason:
                snap = self._last_snap
                fresh = snap is not None and (now - snap.ts).total_seconds() <= 120
                sig = self.ctx.signals() if fresh else None
                dec = self.exit_engine.evaluate(pos, pnl, now, sig, snap.spot if fresh else None)
                reason = dec.reason
                if time.monotonic() - self._last_exit_status > 60:
                    self._last_exit_status = time.monotonic()
                    log.info("MTM %s pnl=%.0f peak=%.0f floor=%s armed=%s giveback=%.0f%% %s", pos.plan.variant_id,
                             pnl, dec.peak, "-" if dec.floor is None else f"{dec.floor:.0f}", dec.armed,
                             dec.giveback * 100, "; ".join(dec.notes))
        else:
            reason = self.risk.update_portfolio(self.capital, self.realized + pnl)
            reason = reason or self.risk.check_position(pos, pnl)
        if not reason and now.time() >= self.cfg.schedule.square_off and (
                self.cfg.execution.product == "MIS" or pos.plan.expiry <= now.date()):
            reason = "SQUARE_OFF_TIME"
        if time.monotonic() - self._last_state_save > 30:
            self._save_state()                         # persists the trailing-stop peak and floor
        if reason:
            self._exit(reason)

    def _margin_fn(self, plan: TradePlan):
        """lots -> margin Kite says the whole structure needs (basket margins, a read-only API call), in BOTH paper and
        live mode so paper only takes trades the account could really carry. Cached ~30 s (the loop runs every second).
        None = the query failed, which blocks the trade (fail closed)."""
        product = self.cfg.execution.product

        def fn(n: int) -> Optional[float]:
            legs = tuple((l.contract, l.side, plan.qty_for(l, n)) for l in plan.legs)
            key = tuple((c.token, s, q) for c, s, q in legs)
            hit = self._margin_cache.get(key)
            if hit and time.monotonic() - hit[0] < 30:
                return hit[1]
            val = self.client.required_margin(list(legs), product)
            if len(self._margin_cache) > 200:
                self._margin_cache.clear()
            self._margin_cache[key] = (time.monotonic(), val)
            return val
        return fn

    def _capital_allows(self, spec) -> bool:
        """Small accounts trade single-leg structures only."""
        return not spec.multi_leg or self.capital >= self.cfg.multi_leg_min_capital

    def _gate(self, plan: TradePlan, lots: int, snap: Snapshot) -> bool:
        """Entry filter (bias, IV skew, OI build-up, reachable trail, costs, intraday theta). Rejections are logged at
        most once a minute. Intraday-signal plans carry their own validated entry logic (intraday.py: levels,
        sideways-day filter, confirmation) and exit on the underlying, so this filter does not apply to them."""
        if plan.spot_rules:
            log.info("ENTRY-FILTER pass %s: intraday signal (%s)", plan.variant_id, plan.rationale)
            return True
        arm = (self.exit_engine.arm_for(plan.max_profit_per_lot * lots)
               if (self.xp.enabled and self.xp.require_reachable_arm) else None)
        cost = _round_trip_cost(plan.legs) * lots
        sched, now = self.cfg.schedule, snap.ts
        session_left = None                                   # None: may be held overnight (NRML before expiry day)
        if self.cfg.execution.product == "MIS" or plan.expiry <= now.date():
            day = lambda t: dt.datetime.combine(now.date(), t, now.tzinfo)
            total = (day(sched.square_off) - day(sched.market_open)).total_seconds()
            session_left = max((day(sched.square_off) - now).total_seconds(), 0.0) / total if total > 0 else 0.0
        dec = evaluate_entry(plan, snap, self.ctx.signals(), self.ep, lots, arm, cost, session_left)
        if dec.ok:
            log.info("ENTRY-FILTER pass %s: %s", plan.variant_id, dec.summary())
            return True
        now_m = time.monotonic()
        if now_m - self._gate_logged.get(plan.variant_id, 0.0) >= 60:
            self._gate_logged[plan.variant_id] = now_m
            log.info("ENTRY-FILTER reject %s: %s", plan.variant_id, dec.reason)
        return False

    def _try_explore(self, now: dt.datetime, snap: Snapshot) -> None:
        """Paper only: trade the scanner's best affordable setup when no approved variant fired (see explorer.py)."""
        if self.live or not self.cfg.paper_explore or self.trades_today >= self.cfg.explore_max_trades:
            return
        if time.monotonic() < self._entry_block_until:
            return
        ok, _why = self.risk.entry_allowed(now, 0)         # window / halt only; the explore cap is applied above
        if not ok:
            return
        levels = None
        if self.xp.enabled:                                  # rank by the levels the exit engine will really use
            levels = lambda p: (self.exit_engine.arm_for(p.max_profit_per_lot),
                                self.exit_engine.stop_for(p.max_loss_per_lot, p.sl_frac))
        specs = [s for s in self.specs if (s.template, s.trend) not in self._explore_block and self._capital_allows(s)]
        pick = pick_explore(snap, specs, self.capital, lambda p: self.risk.size(p, self.capital, self._margin_fn(p)), now,
                            self.cfg.explore_min_score, gate=lambda p, n: self._gate(p, n, snap), levels=levels)
        if pick:
            plan, lots, score = pick
            self._enter(now, snap, plan, lots, score, 1)

    def _try_entry(self, now: dt.datetime, snap: Snapshot) -> None:
        if getattr(self, "pb_enabled", False):
            self._try_playbook(now, snap)
            return
        if not snap.fresh or snap.chain is None:
            return
        usable = any(self._capital_allows(self.spec_by_id[v]) for v in self.approved if v in self.spec_by_id)
        if not usable:                                  # nothing validated for this account size: explore (paper)
            self._try_explore(now, snap)
            return
        ok, why = self.risk.entry_allowed(now, self.trades_today)
        if not ok or time.monotonic() < self._entry_block_until:
            if not ok and self.trades_today >= self.risk.p.max_trades_per_day and self.cfg.explore_with_approved:
                self._try_explore(now, snap)
            return
        candidates: list[tuple[float, TradePlan]] = []
        for vid, score in self.approved.items():
            spec = self.spec_by_id.get(vid)
            if spec is None or not self._capital_allows(spec):
                continue
            plan = build_plan(spec, snap.chain, snap.rs, snap.trend_daily, now.date(), now, snap.features, snap.tinfo,
                              snap.ictx)
            if plan:
                candidates.append((score, plan))
        candidates.sort(key=lambda c: c[0], reverse=True)
        for score, plan in candidates:
            qc = validate_plan_quotes(plan, snap.chain)         # strikes still listed, quoted, liquid and defined-risk
            if not qc.ok:
                log.info("Skip %s: %s", plan.variant_id, qc.reason)
                continue
            lots = self.risk.size(plan, self.capital, self._margin_fn(plan))
            if lots < 1 or not self._gate(plan, lots, snap):
                continue
            self._enter(now, snap, plan, lots, score, len(candidates))
            return
        if self.cfg.explore_with_approved:              # off by default: with validated strategies, wait for them
            self._try_explore(now, snap)

    def _enter(self, now: dt.datetime, snap: Snapshot, plan: TradePlan, lots: int,
               score: float, n_candidates: int) -> None:
        log.info("ENTRY %s lots=%d | %s", plan.variant_id, lots, plan.rationale)
        legs = [OrderLeg(l.contract, l.side, plan.qty_for(l, lots), l.ref_price) for l in plan.legs]
        ts = now.isoformat(timespec="seconds")
        # final pre-trade margin check on the exact structure (paper checks against the paper capital, so a paper trade
        # is only taken if the account could really carry it)
        cash = self.client.available_cash() if self.live else self.capital
        chk = validate_margin(self.client, legs, self.cfg.execution.product, cash, self.risk.p.margin_utilisation)
        if not chk.ok:
            self.alerts.send(f"Entry blocked by margin check for {plan.variant_id}: {chk.reason}",
                             level="ERROR", key="entry-margin")
            self.rec.event(ts, "entry_blocked_margin", plan.variant_id, {"lots": lots, "reason": chk.reason})
            return
        margin_req = chk.required
        fills = self.broker.execute(legs, self.cfg.execution.product)
        if not fills:
            self._entry_failed(ts, plan, lots, legs)
            return
        self._entry_failures = 0
        self.client.subscribe_tokens({l.contract.token: l.contract.key for l in plan.legs})
        self.position = ManagedPosition(
            plan, lots, [OpenLeg(l.contract, l.side, l.qty, fills[l.contract.symbol]) for l in legs],
            now, plan.max_loss_per_lot * lots, plan.max_profit_per_lot * lots)
        self.exit_engine.init_state(self.position, snap.chain, snap.spot)
        self.trades_today += 1
        self._save_state()
        payload = {"lots": lots, "fills": fills, "rationale": plan.rationale, "score": score,
                   "candidates": n_candidates, "spot": snap.spot, "vix": snap.vix,
                   "regime": snap.rs.regime.value, "trend": snap.trend_dir, "margin": margin_req}
        self.rec.event(ts, "entry", plan.variant_id, payload)
        self.rec.trade(ts, self.mode, plan.variant_id, "entry", "", 0.0, 0.0, 0.0, payload)
        self.sheets.append("Orders", ORDERS_HEADER, [
            [ts, self.mode, plan.variant_id, "ENTRY", l.contract.symbol, l.side, l.qty,
             round(l.ref_price, 2), round(fills[l.contract.symbol], 2), plan.rationale[:120]]
            for l in sorted(legs, key=lambda x: x.side != "BUY")])      # rows listed in execution order: hedges first
        mtxt = f", margin {margin_req:,.0f} of {cash:,.0f}" if margin_req else ""
        self.alerts.send(f"ENTRY {plan.name} x{lots} [{plan.variant_id}] {plan.rationale}{mtxt}")
        self._verify_positions({l.contract.symbol: (l.qty if l.side == "BUY" else -l.qty) for l in legs}, "entry")

    def _entry_failed(self, ts: str, plan: TradePlan, lots: int, legs: list[OrderLeg]) -> None:
        """A failed entry was cancelled/unwound by the broker. Never retry immediately (a rejected order would be
        re-sent every loop): cool down, count failures, and halt the day after repeated ones or an unconfirmed unwind."""
        self._entry_failures += 1
        self._entry_block_until = time.monotonic() + 120.0 * self._entry_failures
        self.alerts.send(f"Entry failed for {plan.variant_id} (failure {self._entry_failures} today)",
                         level="ERROR", key="entry-fail")
        self.rec.event(ts, "entry_failed", plan.variant_id, {"lots": lots})
        unresolved = list(getattr(self.broker, "unresolved", []) or [])
        if unresolved:
            self.risk.halt("UNWIND_UNCONFIRMED")
            self.alerts.send(f"UNWIND NOT CONFIRMED for {unresolved}: a partial entry may still be open. "
                             "Trading halted: check and close in Kite NOW.", level="CRITICAL", key="unwind")
        elif self._entry_failures >= 3:
            self.risk.halt("REPEATED_ENTRY_FAILURES")
        self._verify_positions({l.contract.symbol: 0 for l in legs}, "failed entry")   # must be flat

    def _verify_positions(self, expected: dict[str, int], what: str) -> None:
        """Live only: compare the broker's net positions with what the bot believes it holds for these symbols.
        Retries briefly (the positions API can lag a fill). A mismatch halts new entries and raises a CRITICAL alert."""
        if not self.live:
            return
        actual: dict[str, int] = {}
        for attempt in range(3):
            try:
                book = self.client.net_positions()
                actual = {s: book.get(s, 0) for s in expected}
                if actual == expected:
                    return
            except Exception:  # noqa: BLE001 - verification must never break trading
                log.warning("Position verification query failed", exc_info=True)
            time.sleep(1.0)
        self.risk.halt("POSITION_MISMATCH")
        self.alerts.send(f"POSITION MISMATCH after {what}: broker {actual} vs expected {expected}. "
                         "New entries halted: check positions in Kite.", level="CRITICAL", key="pos-mismatch")

    def _clamp_to_broker(self, legs: list[OrderLeg]) -> list[OrderLeg]:
        """Live only: never close more than the broker really holds (e.g. after a POSITION_MISMATCH), or an exit
        would open a naked opposite position."""
        if not self.live:
            return legs
        try:
            book = self.client.net_positions()
        except Exception:  # noqa: BLE001
            log.warning("Could not read broker positions before exit; using the bot's own quantities", exc_info=True)
            return legs
        out = []
        for l in legs:
            held = book.get(l.contract.symbol, 0)
            want = -held if l.side == "BUY" else held            # closing a short = BUY; closing a long = SELL
            qty = min(l.qty, max(want, 0))
            if qty != l.qty:
                log.warning("Exit quantity for %s clamped from %d to %d (broker holds %d)",
                            l.contract.symbol, l.qty, qty, held)
            if qty > 0:
                out.append(OrderLeg(l.contract, l.side, qty, l.ref_price))
        return out

    def _exit(self, reason: str) -> None:
        pos = self.position
        assert pos
        ltps = self._ltps(pos)
        legs = [OrderLeg(l.contract, "SELL" if l.side == "BUY" else "BUY", l.qty, ltps[l.contract.token])
                for l in pos.legs]
        mult = min(1 + self._exit_failures, 8)          # widen the limit buffer after each failure
        legs = self._clamp_to_broker(legs)
        try:
            res = self.broker.close(legs, self.cfg.execution.product, mult,
                                    market=self._exit_failures >= self.cfg.execution.market_exit_after_failures)
        except kex.TokenException:
            raise
        except Exception:  # noqa: BLE001 - an exception must escalate the next attempt (wider limit, then MARKET)
            log.exception("Exit order flow raised")
            self._exit_failures += 1
            self.alerts.send(f"EXIT FAILED ({reason}), attempt {self._exit_failures}: check Kite positions.",
                             level="CRITICAL", key=f"exit-exc-{self._exit_failures}")
            return
        gross, cash = apply_exit_fills(pos.legs, res)   # reduces each leg by what really closed
        cost = trade_cost(cash, self.cfg.costs)
        net = gross - cost
        self.realized += net
        prior_g, prior_c = pos.exit_state.get("closed_gross", 0.0), pos.exit_state.get("closed_cost", 0.0)
        remaining_now = [l for l in pos.legs if l.qty > 0]
        if remaining_now:                              # partial: remember it, report the whole trade at the end
            pos.exit_state["closed_gross"], pos.exit_state["closed_cost"] = prior_g + gross, prior_c + cost
        else:
            gross, cost = prior_g + gross, prior_c + cost
            net = gross - cost
        ts = dt.datetime.now(IST).isoformat(timespec="seconds")
        closed = [o for o in legs if o.contract.symbol in res]
        remaining = [l for l in pos.legs if l.qty > 0]
        tag = "exit" if not remaining else "exit_partial"
        self.rec.trade(ts, self.mode, pos.plan.variant_id, tag, reason, gross, cost, net,
                       {"lots": pos.lots, "day_pnl": self.realized, "remaining": [l.contract.symbol for l in remaining],
                        "explore": pos.plan.rationale.startswith("[EXPLORE"),
                        "setup": pos.plan.rationale.split()[1] if pos.plan.rationale.startswith("[EXPLORE") else "",
                        "max_loss_per_lot": pos.plan.max_loss_per_lot,
                        "peak_pnl": pos.peak_pnl, "floor": pos.exit_state.get("floor")})
        self.sheets.append("Orders", ORDERS_HEADER, [
            [ts, self.mode, pos.plan.variant_id, "EXIT" if not remaining else "EXIT-PARTIAL", o.contract.symbol,
             o.side, res[o.contract.symbol][1], round(o.ref_price, 2), round(res[o.contract.symbol][0], 2), reason]
            for o in closed])
        if remaining:
            pos.legs = remaining
            if self._exit_failures >= 2:               # persistent: stop new risk while it is sorted out
                self.risk.halt("EXIT_NOT_COMPLETING")
            self._exit_failures += 1
            self._save_state()
            left = ", ".join(f"{l.side} {l.contract.symbol} x{l.qty}" for l in remaining)
            self.alerts.send(f"EXIT INCOMPLETE ({reason}), attempt {self._exit_failures}; still open: {left}. "
                             "Retrying with a wider limit, then market orders. Check Kite if this repeats.",
                             level="CRITICAL", key=f"exit-fail-{self._exit_failures}")
            self._verify_positions({l.contract.symbol: (l.qty if l.side == "BUY" else -l.qty) for l in remaining},
                                   "partial exit")
            return
        self.position, self._exit_failures = None, 0
        log.info("EXIT %s reason=%s net=%.0f day=%.0f", pos.plan.variant_id, reason, net, self.realized)
        self.sheets.append("Trades", TRADES_HEADER, [[ts, self.mode, pos.plan.variant_id, reason, pos.lots,
                                                      round(gross, 2), round(cost, 2), round(net, 2),
                                                      round(self.realized, 2)]])
        self._save_state()
        self.alerts.send(f"EXIT {pos.plan.name} [{reason}] net {net:,.0f}; day {self.realized:,.0f}")
        self._verify_positions({o.contract.symbol: 0 for o in legs}, "exit")


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=5)):
        h.setFormatter(fmt)
        root.addHandler(h)


def main() -> int:
    ap = argparse.ArgumentParser(description="SENSEX options bot (Kite Connect)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--paper", action="store_true", help="simulate fills at LTP; no orders sent")
    g.add_argument("--live", action="store_true", help="place real orders")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.log_dir)
    if args.live and os.getenv("LIVE_TRADING_CONFIRM") != "YES":
        log.error("Refusing --live: set LIVE_TRADING_CONFIRM=YES to acknowledge real-money risk")
        return 2

    bot = Bot(cfg, live=args.live)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: bot.stop.set())
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
