"""Telegram commands for the bot: lots, paper / live mode, kill switch and status without a redeploy.

    /lots                 show every index's lot setting
    /lots 3               SENSEX trades use up to 3 lots from the next entry (paper and live)
    /lots NIFTY 2         NIFTY trades use up to 2 lots (any of the six indices)
    /indices              what each index is doing right now (regime, data, last signal)
    /signals              the latest scored signals
    /risk                 risk limits, today's / this week's P&L, breakers, kill switch
    /mode                 show PAPER / LIVE
    /mode live            ask to switch to LIVE: the bot replies with a one-time code
    /mode paper           switch back to paper
    /confirm 123456       confirm a /mode or /resetkill request within 2 minutes
    /kill [reason]        latch the kill switch now (no new entries; an open position is still managed)
    /resetkill            ask to reset a latched kill switch (needs /confirm)
    /status, /report, /help

Lots are an upper bound: an order is reduced to whatever whole number of lots fits the premium cap (config/risk.yaml),
down to one lot, or skipped.

Only messages from the configured TELEGRAM_CHAT_ID are obeyed. /mode, /confirm, /kill and /resetkill additionally
require the sender to be the chat's own user (a private chat) or listed in TELEGRAM_ADMIN_IDS. LIVE is possible only if
the VM's .env has LIVE_TRADING_CONFIRM=YES (the master switch; the bot refuses /mode live otherwise). Settings persist
in data/settings.json; an open position is never resized.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

MAX_LOTS = 20
CONFIRM_TTL_S = 120.0


@dataclass
class LotSettings:
    single_lots: int = 2          # SENSEX lots (name kept so existing settings.json files still load)
    multi_lots: int = 1           # unused since multi-leg trades were removed; kept for file compatibility
    index_lots: dict = field(default_factory=dict)    # other indices: name -> lots


def load_settings(path: str, default_single: int = 2, default_multi: int = 1,
                  index_defaults: Optional[dict] = None) -> LotSettings:
    s = LotSettings(default_single, default_multi, dict(index_defaults or {}))
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        s.single_lots = _valid(d.get("single_lots"), s.single_lots)
        s.multi_lots = _valid(d.get("multi_lots"), s.multi_lots)
        for k, v in (d.get("index_lots") or {}).items():
            if k in s.index_lots:
                s.index_lots[k] = _valid(v, s.index_lots[k])
    except (OSError, ValueError, AttributeError):
        pass
    return s


def save_settings(path: str, s: LotSettings) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".settings-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(s), f)
    os.replace(tmp, path)


def _valid(v, fallback: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    return n if 1 <= n <= MAX_LOTS else fallback


def parse_command(text: str) -> tuple[str, Optional[str]]:
    cmd, args = parse_args(text)
    return cmd, (args[0] if args else None)


def parse_args(text: str) -> tuple[str, list[str]]:
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    return parts[0].split("@")[0].lower(), parts[1:6]          # "/lots@MyBot 3" in group chats


HELP = ("/lots - show lots per index\n/lots N - SENSEX lots\n/lots INDEX N - lots for one index (SENSEX, NIFTY, "
        "BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50)\n/indices - what each index is doing\n/signals - latest signals\n"
        "/risk - limits, P&L, breakers, kill switch\n/mode - paper or live; /mode live|paper then /confirm CODE\n"
        "/kill [reason] - stop new entries now\n/resetkill - reset the kill switch (then /confirm CODE)\n"
        "/status - position and today's P&L\n/report - link to the trading report page")


class TelegramControl:
    """Long-polls getUpdates in a daemon thread. Callbacks: `status_fn` (/status), `indices_fn` (/indices),
    `mode_fn(target)` (a confirmed mode switch), `risk_fn`, `signals_fn`, `kill_fn(reason)`, `reset_kill_fn()`."""

    def __init__(self, token: Optional[str], chat_id: Optional[str], settings_path: str,
                 reply: Callable[[str], None], status_fn: Callable[[], str] = lambda: "",
                 default_single: int = 2, default_multi: int = 1, report_url: Optional[str] = None,
                 index_defaults: Optional[dict] = None, indices_fn: Callable[[], str] = lambda: "",
                 mode_fn: Optional[Callable[[str], str]] = None, mode_now: Callable[[], str] = lambda: "PAPER",
                 admin_ids: Optional[set] = None, risk_fn: Callable[[], str] = lambda: "",
                 signals_fn: Callable[[], str] = lambda: "", kill_fn: Optional[Callable[[str], str]] = None,
                 reset_kill_fn: Optional[Callable[[], str]] = None) -> None:
        self._token, self._chat = token, str(chat_id) if chat_id else None
        self._report_url = report_url
        self.path = settings_path
        self.settings = load_settings(settings_path, default_single, default_multi, index_defaults)
        self._reply, self._status, self._indices = reply, status_fn, indices_fn
        self._mode_fn, self._mode_now = mode_fn, mode_now
        self._risk, self._signals, self._kill, self._reset_kill = risk_fn, signals_fn, kill_fn, reset_kill_fn
        self._admins = {str(a) for a in (admin_ids or set())}
        self._pending: Optional[tuple[str, str, str, float]] = None      # (action, target, code, expires at)
        self._lock = threading.Lock()
        self._offset = 0
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat)

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._loop, daemon=True, name="telegram-control").start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def single_lots(self) -> int:
        with self._lock:
            return self.settings.single_lots

    def lots_for(self, index: str, setup: str = "single") -> int:
        with self._lock:
            if index == "SENSEX":
                return self.settings.single_lots
            return int(self.settings.index_lots.get(index, 1))

    # ---------------------------------------------------------------- commands
    def _authorised(self, chat_id: str, from_id: Optional[str]) -> bool:
        """Owner-only commands: the chat's own user (private chat: chat id == user id) or a listed admin."""
        return from_id is not None and (str(from_id) == str(chat_id) or str(from_id) in self._admins)

    def _ask_confirm(self, action: str, target: str, text: str) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self._lock:
            self._pending = (action, target, code, time.monotonic() + CONFIRM_TTL_S)
        return f"{text} Send /confirm {code} within {int(CONFIRM_TTL_S)} s."

    def handle(self, chat_id: str, text: str, from_id: Optional[str] = None) -> Optional[str]:
        """Apply one command; returns the reply text (None = ignored). Public for tests."""
        if not self._chat or str(chat_id) != self._chat:
            return None                                       # never obey other chats
        cmd, args = parse_args(text)
        if cmd == "/lots":
            return self._lots_cmd(args)
        if cmd == "/mlots":
            return "Multi-leg trades were removed (SENSEX PRO buys single CE/PE options). Use /lots."
        if cmd == "/mode":
            if not args:
                return f"Mode: {self._mode_now()}. Switch with /mode paper or /mode live."
            if not self._authorised(chat_id, from_id):
                return "Only the account owner can change the mode."
            target = args[0].upper()
            if target not in ("PAPER", "LIVE"):
                return "Usage: /mode paper | /mode live"
            if target == self._mode_now():
                return f"Already in {target} mode."
            warn = " LIVE places REAL orders with real money." if target == "LIVE" else ""
            return self._ask_confirm("mode", target, f"Switch to {target}?{warn} The bot restarts in the new mode as "
                                                     "soon as it holds no position.")
        if cmd == "/kill":
            if not self._authorised(chat_id, from_id):
                return "Only the account owner can use the kill switch."
            if self._kill is None:
                return "Kill switch not available."
            return self._kill("manual (Telegram): " + (" ".join(args) or "no reason given"))
        if cmd == "/resetkill":
            if not self._authorised(chat_id, from_id):
                return "Only the account owner can reset the kill switch."
            return self._ask_confirm("resetkill", "", "Reset the kill switch? Only do this after reviewing why it "
                                                      "latched (/risk).")
        if cmd == "/confirm":
            if not self._authorised(chat_id, from_id):
                return "Only the account owner can confirm."
            with self._lock:
                pend, self._pending = self._pending, None
            if not pend or not args or time.monotonic() > pend[3] or not secrets.compare_digest(args[0], pend[2]):
                return "No matching request (wrong or expired code). Start again."
            action, target = pend[0], pend[1]
            if action == "mode":
                if self._mode_fn is None:
                    return "Mode switching is not available."
                log.warning("Mode switch to %s confirmed on Telegram", target)
                return self._mode_fn(target)
            if action == "resetkill":
                if self._reset_kill is None:
                    return "Kill switch reset is not available."
                log.warning("Kill switch reset confirmed on Telegram")
                return self._reset_kill()
            return "Unknown request."
        if cmd == "/indices":
            return self._indices() or "No index status available."
        if cmd == "/signals":
            return self._signals() or "No signals yet today."
        if cmd == "/risk":
            return self._risk() or "No risk status available."
        if cmd == "/status":
            return self._status() or "No status available."
        if cmd == "/report":
            return (f"Trading report (paper and live): {self._report_url}" if self._report_url
                    else "The report link is not configured (REPORT_TOKEN / REPORT_GCS_BUCKET).")
        if cmd in ("/help", "/start"):
            return HELP
        return None

    def _lots_cmd(self, args: list[str]) -> str:
        if not args:
            return self._lots_text()
        index = "SENSEX"
        if len(args) >= 2 and not args[0].isdigit():
            index, args = args[0].upper(), args[1:]
            if index != "SENSEX" and index not in self.settings.index_lots:
                return f"Unknown index {index}. Indices: SENSEX, {', '.join(sorted(self.settings.index_lots))}"
        try:
            n = int(args[0])
        except ValueError:
            return f"Usage: /lots <1-{MAX_LOTS}> or /lots <INDEX> <N>"
        if not 1 <= n <= MAX_LOTS:
            return f"Lots must be between 1 and {MAX_LOTS}."
        with self._lock:
            if index == "SENSEX":
                self.settings.single_lots = n
            else:
                self.settings.index_lots[index] = n
            try:
                save_settings(self.path, self.settings)
            except OSError:
                log.exception("Could not save lot settings")
                return "Changed for this run, but saving failed (disk?): a restart would revert it."
        log.info("Lot settings changed by Telegram: %s", asdict(self.settings))
        return "OK. " + self._lots_text() + " (applies from the next entry; reduced to fit the premium cap)"

    def _lots_text(self) -> str:
        s = self.settings
        rest = ", ".join(f"{k} {v}" for k, v in sorted(s.index_lots.items()))
        return f"Lots: SENSEX {s.single_lots}" + (f", {rest}" if rest else "")

    def _loop(self) -> None:
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        while not self._stop.is_set():
            try:
                r = requests.get(url, params={"offset": self._offset, "timeout": 25,
                                              "allowed_updates": json.dumps(["message"])}, timeout=35)
                if not r.ok:
                    log.warning("Telegram getUpdates HTTP %s", r.status_code)
                    self._stop.wait(30)
                    continue
                for u in r.json().get("result", []):
                    self._offset = max(self._offset, int(u.get("update_id", 0)) + 1)
                    msg = u.get("message") or {}
                    sender = (msg.get("from") or {}).get("id")
                    try:
                        out = self.handle(str((msg.get("chat") or {}).get("id", "")), msg.get("text", ""),
                                          str(sender) if sender is not None else None)
                    except Exception:  # noqa: BLE001 - a broken command must not kill the poller
                        log.exception("Telegram command failed")
                        out = "That command failed; see the bot logs."
                    if out:
                        self._reply(out)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram command poll failed: %s", exc.__class__.__name__)   # URL holds the token
                self._stop.wait(15)
