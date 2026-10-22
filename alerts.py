"""Telegram alerting. A no-op (log only) when credentials are not configured."""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, token: Optional[str], chat_id: Optional[str], dedupe_s: float = 300.0) -> None:
        self._token, self._chat = token, chat_id
        self._dedupe_s = dedupe_s
        self._last: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat)

    def send(self, text: str, level: str = "INFO", key: Optional[str] = None) -> None:
        """Send a message. ``key`` suppresses repeats of the same alert for dedupe_s."""
        log.log(logging.CRITICAL if level == "CRITICAL" else logging.INFO, "ALERT[%s] %s", level, text)
        if not self.enabled:
            return
        if key:
            now = time.monotonic()
            if now - self._last.get(key, -1e9) < self._dedupe_s:
                return
            self._last[key] = now
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": f"[{level}] {text}"[:4000],
                      "disable_web_page_preview": True},
                timeout=5,
            )
            if not r.ok:   # e.g. 401 bad token, 400/403 wrong chat id or bot never started
                try:
                    desc = r.json().get("description", "")
                except ValueError:
                    desc = ""
                log.warning("Telegram rejected the message (HTTP %s): %s", r.status_code, desc)
        except requests.RequestException as exc:
            log.warning("Telegram send failed: %s", exc.__class__.__name__)  # class only: URL holds the bot token
