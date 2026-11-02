"""Telegram commands for the bot: lot sizes and paper / live mode without a redeploy.

    /lots                 show every lot setting
    /lots 3               SENSEX single-leg trades use 3 lots from the next entry (paper and live)
    /mlots 1              SENSEX multi-leg trades use 1 lot from the next entry
    /lots NIFTY 2         NIFTY trades use 2 lots (any scanned index: SENSEX sets both its single and multi lots)
    /indices              what each index is doing right now
    /mode                 show PAPER / LIVE
    /mode live            ask to switch to LIVE: the bot replies with a one-time code
    /confirm 123456       confirm within 2 minutes; the bot restarts in the new mode once it holds no position
    /mode paper           switch back to paper (also needs /confirm)
    /status, /report, /help

Only messages from the configured TELEGRAM_CHAT_ID are obeyed. /mode and /confirm additionally require the sender to
be the chat's own user (a private chat) or listed in TELEGRAM_ADMIN_IDS, so a member of a group chat cannot switch the
account to live. LIVE is possible only if the VM's .env has LIVE_TRADING_CONFIRM=YES (the master switch; the bot
refuses /mode live otherwise). Settings persist in data/settings.json; an open position is never resized.
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
    single_lots: int = 2          # SENSEX single-leg, in BOTH paper and live
    multi_lots: int = 1           # SENSEX multi-leg
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
    return parts[0].split("@")[0].lower(), parts[1:4]          # "/lots@MyBot 3" in group chats


class TelegramControl:
    """Long-polls getUpdates in a daemon thread. `status_fn` returns a status line for /status, `indices_fn` the
    per-index line for /indices; `mode_fn(target)` performs a confirmed mode switch and returns the reply."""

    def __init__(self, token: Optional[str], chat_id: Optional[str], settings_path: str,
                 reply: Callable[[str], None], status_fn: Callable[[], str] = lambda: "",
                 default_single: int = 2, default_multi: int = 1, report_url: Optional[str] = None,
                 index_defaults: Optional[dict] = None, indices_fn: Callable[[], str] = lambda: "",
                 mode_fn: Optional[Callable[[str], str]] = None, mode_now: Callable[[], str] = lambda: "PAPER",
                 admin_ids: Optional[set] = None) -> None:
        self._token, self._chat = token, str(chat_id) if chat_id else None
        self._report_url = report_url
        self.path = settings_path
        self.settings = load_settings(settings_path, default_single, default_multi, index_defaults)
        self._reply, self._status, self._indices = reply, status_fn, indices_fn
        self._mode_fn, self._mode_now = mode_fn, mode_now
        self._admins = {str(a) for a in (admin_ids or set())}
        self._pending: Optional[tuple[str, str, float]] = None      # (target mode, code, expires at)
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

    @property
    def multi_lots(self) -> int:
        with self._lock:
            return self.settings.multi_lots

    def lots_for(self, index: str, setup: str) -> int:
        """Lots for a trade: SENSEX keeps its single / multi settings; every other index has one number."""
        with self._lock:
            if index == "SENSEX":
                return self.settings.single_lots if setup == "single" else self.settings.multi_lots
            return int(self.settings.index_lots.get(index, 1))

    # ---------------------------------------------------------------- commands
    def _authorised(self, chat_id: str, from_id: Optional[str]) -> bool:
        """Mode changes: the chat's own user (private chat: chat id == user id) or a listed admin."""
        return from_id is not None and (str(from_id) == str(chat_id) or str(from_id) in self._admins)

    def handle(self, chat_id: str, text: str, from_id: Optional[str] = None) -> Optional[str]:
        """Apply one command; returns the reply text (None = ignored). Public for tests."""
        if not self._chat or str(chat_id) != self._chat:
            return None                                       # never obey other chats
        cmd, args = parse_args(text)
        if cmd in ("/lots", "/mlots"):
            return self._lots_cmd(cmd, args)
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
            code = f"{secrets.randbelow(10 ** 6):06d}"
            with self._lock:
                self._pending = (target, code, time.monotonic() + CONFIRM_TTL_S)
            warn = (" LIVE places REAL orders with real money." if target == "LIVE" else "")
            return (f"Switch to {target}?{warn} Send /confirm {code} within {int(CONFIRM_TTL_S)} s. The bot restarts "
                    "in the new mode as soon as it holds no position.")
        if cmd == "/confirm":
            if not self._authorised(chat_id, from_id):
                return "Only the account owner can change the mode."
            with self._lock:
                pend, self._pending = self._pending, None
            if not pend or not args or time.monotonic() > pend[2] or not secrets.compare_digest(args[0], pend[1]):
                return "No matching request (wrong or expired code). Start again with /mode."
            if self._mode_fn is None:
                return "Mode switching is not available."
            log.warning("Mode switch to %s confirmed on Telegram", pend[0])
            return self._mode_fn(pend[0])
        if cmd == "/indices":
            return self._indices() or "No index status available."
        if cmd == "/status":
            return self._status() or "No status available."
        if cmd == "/report":
            return (f"Trading report (paper and live): {self._report_url}" if self._report_url
                    else "The report link is not configured (BOT_DOMAIN / REPORT_TOKEN).")
        if cmd in ("/help", "/start"):
            return ("/lots - show lots\n/lots N - SENSEX single-leg lots\n/mlots N - SENSEX multi-leg lots\n"
                    "/lots INDEX N - lots for one index (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX)\n"
                    "/indices - what each index is doing\n/mode - paper or live; /mode live|paper then /confirm CODE\n"
                    "/status - position and today's P&L\n/report - link to the trading report page")
        return None

    def _lots_cmd(self, cmd: str, args: list[str]) -> str:
        if not args:
            return self._lots_text()
        index = None
        if cmd == "/lots" and len(args) >= 2 and not args[0].isdigit():
            index, args = args[0].upper(), args[1:]
            if index != "SENSEX" and index not in self.settings.index_lots:
                return f"Unknown index {index}. Indices: SENSEX, {', '.join(sorted(self.settings.index_lots))}"
        try:
            n = int(args[0])
        except ValueError:
            return f"Usage: {cmd} <1-{MAX_LOTS}>" + (" or /lots <INDEX> <N>" if cmd == "/lots" else "")
        if not 1 <= n <= MAX_LOTS:
            return f"Lots must be between 1 and {MAX_LOTS}."
        with self._lock:
            if index == "SENSEX":
                self.settings.single_lots = self.settings.multi_lots = n
            elif index:
                self.settings.index_lots[index] = n
            elif cmd == "/lots":
                self.settings.single_lots = n
            else:
                self.settings.multi_lots = n
            try:
                save_settings(self.path, self.settings)
            except OSError:
                log.exception("Could not save lot settings")
                return "Changed for this run, but saving failed (disk?): a restart would revert it."
        log.info("Lot settings changed by Telegram: %s", asdict(self.settings))
        return "OK. " + self._lots_text() + " (applies from the next entry)"

    def _lots_text(self) -> str:
        s = self.settings
        rest = ", ".join(f"{k} {v}" for k, v in sorted(s.index_lots.items()))
        base = f"Single-leg lots: {s.single_lots}, multi-leg lots: {s.multi_lots}"
        return base + (f" (SENSEX); {rest}" if rest else "")

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
                    out = self.handle(str((msg.get("chat") or {}).get("id", "")), msg.get("text", ""),
                                      str(sender) if sender is not None else None)
                    if out:
                        self._reply(out)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram command poll failed: %s", exc.__class__.__name__)   # URL holds the token
                self._stop.wait(15)
