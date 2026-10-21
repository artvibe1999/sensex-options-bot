"""Optional live feed of orders and results to a Google Sheet.

The bot POSTs rows to a tiny Google Apps Script web app that appends them to your
spreadsheet (script in deploy/google_sheets_script.gs). No Google credentials or
API keys are stored on the server: only the web-app URL and a shared secret.

Design: fire-and-forget. A background thread sends rows; a slow or failing Sheet
never delays or breaks trading. After 5 failed attempts a batch is dropped (the
SQLite database remains the complete record).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional, Sequence

import requests

log = logging.getLogger("sheets")

ORDERS_HEADER = ["Time (IST)", "Mode", "Strategy variant", "Action", "Instrument", "Side", "Qty",
                 "Ref price (LTP)", "Fill price", "Reason"]
TRADES_HEADER = ["Exit time (IST)", "Mode", "Strategy variant", "Reason", "Lots", "Gross P&L",
                 "Costs", "Net P&L", "Day P&L"]
STATUS_HEADER = ["Time (IST)", "Mode", "Event", "Details"]


class SheetsSink:
    def __init__(self, url: Optional[str], secret: Optional[str]) -> None:
        self.enabled = bool(url and secret)
        self._url, self._secret = url, secret
        self._q: "queue.Queue[tuple[str, Sequence[str], list]]" = queue.Queue(maxsize=1000)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="sheets").start()
            log.info("Google Sheets feed enabled")
        else:
            log.info("Google Sheets feed disabled (SHEETS_WEBHOOK_URL / SHEETS_SECRET not set)")

    def append(self, tab: str, header: Sequence[str], rows: list) -> None:
        if not self.enabled or not rows:
            return
        try:
            self._q.put_nowait((tab, header, rows))
        except queue.Full:
            log.warning("Sheets queue full: dropping %d rows", len(rows))

    def _worker(self) -> None:
        while True:
            tab, header, rows = self._q.get()
            for attempt in range(5):
                try:
                    r = requests.post(self._url, json={"secret": self._secret, "tab": tab,
                                                       "header": list(header), "rows": rows}, timeout=20)
                    if r.ok and r.text.strip() == "ok":
                        break
                    log.warning("Sheets rejected rows (HTTP %s): check the script URL/secret", r.status_code)
                except requests.RequestException as exc:
                    log.warning("Sheets post failed: %s", exc.__class__.__name__)
                time.sleep(2 ** attempt)
            else:
                log.error("Dropped %d rows for tab %s after 5 failures", len(rows), tab)
