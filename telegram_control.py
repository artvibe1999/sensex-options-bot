"""Telegram commands for the bot: change lot sizes without a redeploy.

    /lots            show the current lot settings
    /lots 3          single-leg trades use 3 lots from the next entry (paper and live)
    /mlots 1         multi-leg trades use 1 lot from the next entry
    /status          mode, open position, today's P&L
    /help

Only messages from the configured TELEGRAM_CHAT_ID are obeyed; anything else is ignored. Settings persist in
data/settings.json, so a restart keeps them. An open position is never resized: the change applies to the next entry.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

MAX_LOTS = 20


@dataclass
class LotSettings:
    single_lots: int = 2          # default for single-leg trades in BOTH paper and live
    multi_lots: int = 1


def load_settings(path: str, default_single: int = 2, default_multi: int = 1) -> LotSettings:
    s = LotSettings(default_single, default_multi)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        s.single_lots = _valid(d.get("single_lots"), s.single_lots)
        s.multi_lots = _valid(d.get("multi_lots"), s.multi_lots)
    except (OSError, ValueError):
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
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", None
    cmd = parts[0].split("@")[0].lower()          # "/lots@MyBot 3" in group chats
    return cmd, (parts[1] if len(parts) > 1 else None)


class TelegramControl:
    """Long-polls getUpdates in a daemon thread. `status_fn` returns a status line for /status."""

    def __init__(self, token: Optional[str], chat_id: Optional[str], settings_path: str,
                 reply: Callable[[str], None], status_fn: Callable[[], str] = lambda: "",
                 default_single: int = 2, default_multi: int = 1) -> None:
        self._token, self._chat = token, str(chat_id) if chat_id else None
        self.path = settings_path
        self.settings = load_settings(settings_path, default_single, default_multi)
        self._reply, self._status = reply, status_fn
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

    # ---------------------------------------------------------------- commands
    def handle(self, chat_id: str, text: str) -> Optional[str]:
        """Apply one command; returns the reply text (None = ignored). Public for tests."""
        if not self._chat or str(chat_id) != self._chat:
            return None                                       # never obey other chats
        cmd, arg = parse_command(text)
        if cmd in ("/lots", "/mlots"):
            if arg is None:
                return self._lots_text()
            try:
                n = int(arg)
            except ValueError:
                return f"Usage: {cmd} <1-{MAX_LOTS}>"
            if not 1 <= n <= MAX_LOTS:
                return f"Lots must be between 1 and {MAX_LOTS}."
            with self._lock:
                if cmd == "/lots":
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
        if cmd == "/status":
            return self._status() or "No status available."
        if cmd in ("/help", "/start"):
            return ("/lots - show lots\n/lots N - single-leg lots (paper and live)\n/mlots N - multi-leg lots\n"
                    "/status - position and today's P&L")
        return None

    def _lots_text(self) -> str:
        s = self.settings
        return f"Single-leg lots: {s.single_lots}, multi-leg lots: {s.multi_lots}"

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
                    out = self.handle(str((msg.get("chat") or {}).get("id", "")), msg.get("text", ""))
                    if out:
                        self._reply(out)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram command poll failed: %s", exc.__class__.__name__)   # URL holds the token
                self._stop.wait(15)
