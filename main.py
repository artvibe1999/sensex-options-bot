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
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Optional

from kiteconnect import exceptions as kex

from alerts import Alerter
from config import IST, Config, load_config
from costs import trade_cost
from market_data import MarketData, Snapshot
from recorder import Recorder
from health_report import HEALTH_HEADER, collect as health_collect, sheet_row as health_row
from risk_manager import ManagedPosition, OpenLeg, RiskManager
from sheets_sink import ORDERS_HEADER, STATUS_HEADER, TRADES_HEADER, SheetsSink
from shadow import ShadowBook
from strategy_engine import Leg, RegimeDetector, StrategyEngine, TradePlan
from strategy_library import build_library, build_plan, load_approved
from zerodha_client import AuthError, OptionContract, ZerodhaClient

log = logging.getLogger("bot")


@dataclass(frozen=True)
class OrderLeg:
    contract: OptionContract
    side: str
    qty: int
    ref_price: float


def _round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)


# ------------------------------------------------------------------ brokers
class PaperBroker:
    """Simulated fills at LTP +/- slippage; no API orders are placed."""

    def __init__(self, slippage_pct: float) -> None:
        self.slip = slippage_pct

    def execute(self, legs: list[OrderLeg], product: str, buffer_mult: float = 1.0) -> Optional[dict[str, float]]:
        fills = {}
        for l in legs:
            f = l.ref_price * (1 + self.slip if l.side == "BUY" else 1 - self.slip)
            fills[l.contract.symbol] = _round_tick(f, l.contract.tick_size)
            log.info("[PAPER] %s %s x%d @ %.2f", l.side, l.contract.symbol, l.qty, fills[l.contract.symbol])
        return fills


class LiveBroker:
    """Real orders: marketable LIMITs, BUY legs first, unwind on partial failure."""

    def __init__(self, client: ZerodhaClient, cfg: Config) -> None:
        self.client, self.x = client, cfg.execution

    def _limit(self, l: OrderLeg, mult: float = 1.0) -> float:
        b = min(self.x.limit_buffer_pct * mult, self.x.max_exit_buffer_pct)
        return _round_tick(l.ref_price * (1 + b if l.side == "BUY" else 1 - b), l.contract.tick_size)

    def _wait(self, order_id: str) -> tuple[str, float, int]:
        deadline = time.monotonic() + self.x.fill_timeout_s
        status, avg, filled = "OPEN", 0.0, 0
        while time.monotonic() < deadline:
            h = self.client.order_history(order_id)[-1]
            status, avg, filled = h["status"], float(h.get("average_price") or 0), int(h.get("filled_quantity") or 0)
            if status in ("COMPLETE", "REJECTED", "CANCELLED"):
                break
            time.sleep(0.5)
        return status, avg, filled

    def execute(self, legs: list[OrderLeg], product: str, buffer_mult: float = 1.0) -> Optional[dict[str, float]]:
        done: list[tuple[OrderLeg, float, int]] = []
        for l in sorted(legs, key=lambda x: x.side != "BUY"):  # BUY first limits margin/risk
            try:
                oid = self.client.place_order(l.contract, l.side, l.qty, product,
                                              self._limit(l, buffer_mult), self.x.tag)
            except Exception:  # noqa: BLE001
                log.exception("Order placement failed for %s", l.contract.symbol)
                self._unwind(done, product)
                return None
            status, avg, filled = self._wait(oid)
            if status != "COMPLETE":
                try:
                    self.client.cancel_order(oid)
                except Exception:  # noqa: BLE001
                    log.exception("Cancel failed for %s", oid)
                status, avg, filled = self._wait(oid)
                if filled:
                    done.append((l, avg, filled))
                log.error("Leg %s not filled (%s); unwinding", l.contract.symbol, status)
                self._unwind(done, product)
                return None
            done.append((l, avg, filled))
            log.info("[LIVE] %s %s x%d @ %.2f (order %s)", l.side, l.contract.symbol, l.qty, avg, oid)
        return {l.contract.symbol: avg for l, avg, _ in done}

    def _unwind(self, done: list[tuple[OrderLeg, float, int]], product: str) -> None:
        for l, _avg, qty in done:
            side = "SELL" if l.side == "BUY" else "BUY"
            rev = OrderLeg(l.contract, side, qty, l.ref_price)
            try:
                oid = self.client.place_order(l.contract, side, qty, product, self._limit(rev, 3.0), self.x.tag)
                log.warning("Unwind order %s for %s", oid, l.contract.symbol)
            except Exception:  # noqa: BLE001
                log.critical("UNWIND FAILED for %s: close manually!", l.contract.symbol, exc_info=True)


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
                 "legs": [{"contract": _contract_to_dict(l.contract), "side": l.side, "ref_price": l.ref_price}
                          for l in p.legs],
                 "lot_size": p.lot_size, "max_loss_unit": p.max_loss_unit,
                 "max_profit_unit": p.max_profit_unit, "rationale": p.rationale,
                 "variant_id": p.variant_id, "sl_frac": p.sl_frac, "tp_frac": p.tp_frac},
        "lots": pos.lots,
        "legs": [{"contract": _contract_to_dict(l.contract), "side": l.side, "qty": l.qty,
                  "entry_price": l.entry_price} for l in pos.legs],
        "entered_at": pos.entered_at.isoformat(), "max_loss": pos.max_loss,
        "max_profit": pos.max_profit, "peak_pnl": pos.peak_pnl,
    }


def _position_from_dict(d: dict) -> ManagedPosition:
    p = d["plan"]
    plan = TradePlan(
        name=p["name"], regime=p["regime"], expiry=dt.date.fromisoformat(p["expiry"]),
        legs=tuple(Leg(_contract_from_dict(l["contract"]), l["side"], l["ref_price"]) for l in p["legs"]),
        lot_size=p["lot_size"], max_loss_unit=p["max_loss_unit"], max_profit_unit=p["max_profit_unit"],
        rationale=p["rationale"], variant_id=p.get("variant_id", ""),
        sl_frac=p.get("sl_frac"), tp_frac=p.get("tp_frac"))
    legs = [OpenLeg(_contract_from_dict(l["contract"]), l["side"], l["qty"], l["entry_price"]) for l in d["legs"]]
    return ManagedPosition(plan, d["lots"], legs, dt.datetime.fromisoformat(d["entered_at"]),
                           d["max_loss"], d["max_profit"], d.get("peak_pnl", 0.0))


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
        self.broker = LiveBroker(self.client, cfg) if live else PaperBroker(cfg.execution.paper_slippage_pct)
        self.alerts = Alerter(cfg.alerts.telegram_token, cfg.alerts.telegram_chat_id)
        self.rec = Recorder(cfg.paths.db)
        self.sheets = SheetsSink(cfg.sheets_url, cfg.sheets_secret)
        self.specs = build_library()
        self.spec_by_id = {s.id: s for s in self.specs}
        self.shadow = ShadowBook(self.specs, self.rec, cfg.risk, cfg.schedule, cfg.costs)
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
        self._last_state_save = 0.0
        self._last_heartbeat = 0.0
        self._approved_stamp: Optional[tuple] = None
        self._load_state()

    # ------------------------------------------------------------ persistence
    def _save_state(self) -> None:
        doc = {"date": (self.day or dt.date.today()).isoformat(), "mode": self.mode,
               "realized": self.realized, "trades_today": self.trades_today,
               "risk": self.risk.export(),
               "position": _position_to_dict(self.position) if self.position else None}
        path = self.cfg.paths.state
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        self._last_state_save = time.monotonic()

    def _load_state(self) -> None:
        try:
            with open(self.cfg.paths.state, encoding="utf-8") as f:
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
            self.risk.restore(doc.get("risk", {}))
            if doc.get("position"):
                self.position = _position_from_dict(doc["position"])
                log.warning("Restored open position %s from state file", self.position.plan.name)
        except (KeyError, ValueError, TypeError):
            log.exception("State file unreadable; starting clean")
            self.position = None

    # -------------------------------------------------------------- lifecycle
    def run(self) -> None:
        self.alerts.send(f"Bot started in {self.mode} mode", key="start")
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
            self.stop.wait(self.cfg.loop_interval_s)
        if self.position:
            log.warning("Shutting down with open position; flattening")
            try:
                self._exit("SHUTDOWN")
            except Exception:  # noqa: BLE001
                log.critical("Could not flatten on shutdown: check positions manually", exc_info=True)
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
            return                                    # pre-open, holiday or stalled feed
        snap = self.md.snapshot(now)
        if snap.fresh:
            self.rec.record_snapshot(snap)
            self.shadow.step(snap, now)
        if self.position:
            self._manage(now)
        elif not self.risk.halted:
            self._try_entry(now, snap)
        if self.risk.halted and not self._halt_alerted:
            self._halt_alerted = True
            self.alerts.send(f"Trading halted: {self.risk.halt_reason}", level="CRITICAL")

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
        path = self.cfg.paths.approved_live if self.live else self.cfg.paths.approved_backtest
        try:
            mtime: Optional[float] = os.stat(path).st_mtime
        except OSError:
            mtime = None
        stamp = (mtime, now.date())
        if stamp == self._approved_stamp:
            return
        self._approved_stamp = stamp
        self.approved = load_approved(path, now.date())
        if self.approved:
            log.info("%d approved variants loaded from %s", len(self.approved), path)
            self.alerts.send(f"{len(self.approved)} approved variants loaded", key="approved-loaded")
        else:
            log.warning("No valid approved variants in %s: shadow data collection only, no trades", path)
            self.alerts.send("No approved strategies: only shadow data collection is running", key="noapprove")

    def _new_day(self, now: dt.datetime) -> None:
        log.info("New trading day %s", now.date())
        self.day = now.date()
        self.realized, self.trades_today = 0.0, 0
        self.risk.reset_day()
        self.shadow.reset_day()
        self.md.reset_day()
        self.session_ready = self.eod_done = False
        self._auth_alerted = self._halt_alerted = False
        self._exit_failures = 0
        self._approved_stamp = None            # force a reload below
        self._refresh_approvals(now)
        self._save_state()

    def _ensure_session(self, now: dt.datetime) -> bool:
        try:
            self.client.authenticate()
        except AuthError as exc:
            if not self._auth_alerted:
                self._auth_alerted = True
                self.alerts.send(f"Not logged in to Kite: {exc}", level="CRITICAL", key="auth")
            self.stop.wait(30)
            return False
        self.client.load_instruments()
        self.client.start_stream()
        if self.position:
            self.client.subscribe_tokens({l.contract.token: l.contract.key for l in self.position.legs})
        self.capital = self.client.available_cash() if self.live else self.cfg.paper_capital
        log.info("Session ready: mode=%s capital=%.0f", self.mode, self.capital)
        if self.live:
            self._reconcile()
        self.session_ready = True
        self.sheets.append("Status", STATUS_HEADER, [[now.isoformat(timespec="seconds"), self.mode,
                                                      "SESSION_READY", f"capital {self.capital:,.0f}, "
                                                      f"{len(self.approved)} approved variants"]])
        self.alerts.send(f"Session ready ({self.mode}), capital {self.capital:,.0f}, "
                         f"{len(self.approved)} approved variants", key="session")
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
        self.rec.event(now.isoformat(timespec="seconds"), "day_summary", "",
                       {"realized": self.realized, "trades": self.trades_today, "mode": self.mode})
        self.alerts.send(f"Day done ({self.mode}): net PnL {self.realized:,.0f} over {self.trades_today} trades")
        self.sheets.append("Status", STATUS_HEADER, [[now.isoformat(timespec="seconds"), self.mode, "DAY_DONE",
                                                      f"net {self.realized:,.0f} over {self.trades_today} trades"]])
        self.client.stop_stream()
        self.session_ready, self.eod_done = False, True
        self._save_state()

    # ---------------------------------------------------------------- trading
    def _ltps(self, pos: ManagedPosition) -> dict[int, float]:
        return {l.contract.token: self.client.get_ltp(l.contract.token) for l in pos.legs}

    def _manage(self, now: dt.datetime) -> None:
        pos = self.position
        assert pos
        pnl = pos.pnl(self._ltps(pos))
        reason = self.risk.update_portfolio(self.capital, self.realized + pnl)
        reason = reason or self.risk.check_position(pos, pnl)
        if not reason and now.time() >= self.cfg.schedule.square_off and (
                self.cfg.execution.product == "MIS" or pos.plan.expiry <= now.date()):
            reason = "SQUARE_OFF_TIME"
        if time.monotonic() - self._last_state_save > 30:
            self._save_state()                         # persists the trailing-stop peak
        if reason:
            self._exit(reason)

    def _try_entry(self, now: dt.datetime, snap: Snapshot) -> None:
        if not snap.fresh or snap.chain is None or not self.approved:
            return
        ok, why = self.risk.entry_allowed(now, self.trades_today)
        if not ok:
            return
        candidates: list[tuple[float, TradePlan]] = []
        for vid, score in self.approved.items():
            spec = self.spec_by_id.get(vid)
            plan = build_plan(spec, snap.chain, snap.rs, snap.trend_dir, now.date(), now) if spec else None
            if plan:
                candidates.append((score, plan))
        candidates.sort(key=lambda c: c[0], reverse=True)
        product = self.cfg.execution.product
        for score, plan in candidates:
            margin_fn = None
            if self.live:
                margin_fn = lambda n, plan=plan: self.client.required_margin(
                    [(l.contract, l.side, n * plan.lot_size) for l in plan.legs], product)
            lots = self.risk.size(plan, self.capital, margin_fn)
            if lots < 1:
                continue
            self._enter(now, snap, plan, lots, score, len(candidates))
            return

    def _enter(self, now: dt.datetime, snap: Snapshot, plan: TradePlan, lots: int,
               score: float, n_candidates: int) -> None:
        log.info("ENTRY %s lots=%d | %s", plan.variant_id, lots, plan.rationale)
        legs = [OrderLeg(l.contract, l.side, lots * plan.lot_size, l.ref_price) for l in plan.legs]
        fills = self.broker.execute(legs, self.cfg.execution.product)
        ts = now.isoformat(timespec="seconds")
        if not fills:
            self.alerts.send(f"Entry failed for {plan.variant_id}", level="ERROR", key="entry-fail")
            self.rec.event(ts, "entry_failed", plan.variant_id, {"lots": lots})
            return
        self.client.subscribe_tokens({l.contract.token: l.contract.key for l in plan.legs})
        self.position = ManagedPosition(
            plan, lots, [OpenLeg(l.contract, l.side, l.qty, fills[l.contract.symbol]) for l in legs],
            now, plan.max_loss_per_lot * lots, plan.max_profit_per_lot * lots)
        self.trades_today += 1
        self._save_state()
        payload = {"lots": lots, "fills": fills, "rationale": plan.rationale, "score": score,
                   "candidates": n_candidates, "spot": snap.spot, "vix": snap.vix,
                   "regime": snap.rs.regime.value, "trend": snap.trend_dir}
        self.rec.event(ts, "entry", plan.variant_id, payload)
        self.rec.trade(ts, self.mode, plan.variant_id, "entry", "", 0.0, 0.0, 0.0, payload)
        self.sheets.append("Orders", ORDERS_HEADER, [
            [ts, self.mode, plan.variant_id, "ENTRY", l.contract.symbol, l.side, l.qty,
             round(l.ref_price, 2), round(fills[l.contract.symbol], 2), plan.rationale[:120]] for l in legs])
        self.alerts.send(f"ENTRY {plan.name} x{lots} [{plan.variant_id}] {plan.rationale}")

    def _exit(self, reason: str) -> None:
        pos = self.position
        assert pos
        ltps = self._ltps(pos)
        legs = [OrderLeg(l.contract, "SELL" if l.side == "BUY" else "BUY", l.qty, ltps[l.contract.token])
                for l in pos.legs]
        mult = min(1 + self._exit_failures, 8)          # widen the limit buffer after each failure
        fills = self.broker.execute(legs, self.cfg.execution.product, mult)
        if not fills:
            self._exit_failures += 1
            self.alerts.send(f"EXIT FAILED ({reason}), attempt {self._exit_failures}; retrying with a "
                             "wider limit. Check positions manually if this repeats.",
                             level="CRITICAL", key=f"exit-fail-{self._exit_failures}")
            return
        gross, cash = 0.0, []
        for l in pos.legs:
            px = fills[l.contract.symbol]
            gross += (px - l.entry_price) * l.qty * (1 if l.side == "BUY" else -1)
            cash += [(l.side, l.entry_price, l.qty),
                     ("SELL" if l.side == "BUY" else "BUY", px, l.qty)]
        cost = trade_cost(cash, self.cfg.costs)
        net = gross - cost
        self.realized += net
        self.position, self._exit_failures = None, 0
        ts = dt.datetime.now(IST).isoformat(timespec="seconds")
        log.info("EXIT %s reason=%s net=%.0f day=%.0f", pos.plan.variant_id, reason, net, self.realized)
        self.rec.trade(ts, self.mode, pos.plan.variant_id, "exit", reason, gross, cost, net,
                       {"lots": pos.lots, "day_pnl": self.realized})
        self.sheets.append("Orders", ORDERS_HEADER, [
            [ts, self.mode, pos.plan.variant_id, "EXIT", o.contract.symbol, o.side, o.qty,
             round(o.ref_price, 2), round(fills[o.contract.symbol], 2), reason] for o in legs])
        self.sheets.append("Trades", TRADES_HEADER, [[ts, self.mode, pos.plan.variant_id, reason, pos.lots,
                                                      round(gross, 2), round(cost, 2), round(net, 2),
                                                      round(self.realized, 2)]])
        self._save_state()
        self.alerts.send(f"EXIT {pos.plan.name} [{reason}] net {net:,.0f}; day {self.realized:,.0f}")


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
