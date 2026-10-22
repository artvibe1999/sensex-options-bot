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
import uuid
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
            batch_id = uuid.uuid4().hex             # lets the script ignore a retried batch (no duplicate rows)
            for attempt in range(4):
                try:
                    r = requests.post(self._url, json={"secret": self._secret, "tab": tab, "id": batch_id,
                                                       "header": list(header), "rows": rows}, timeout=30)
                    body = r.text.strip()
                    if body == "ok":
                        break
                    if r.status_code == 200 and "forbidden" not in body and "<html" not in body.lower():
                        # Unexpected but non-error reply: the script ran. Do NOT retry (would duplicate rows).
                        log.warning("Sheets reply was %r instead of 'ok'; treating as delivered", body[:60])
                        break
                    log.warning("Sheets did not accept rows (HTTP %s, reply %r): check script URL/secret/deployment",
                                r.status_code, body[:60])
                except requests.RequestException as exc:
                    log.warning("Sheets post failed: %s", exc.__class__.__name__)
                time.sleep(2 ** attempt)
            else:
                log.error("Dropped %d rows for tab %s after 4 failures", len(rows), tab)


def _explain(r: "requests.Response") -> None:
    """Print how Google answered (redirect hops, content type, visible text) with no secrets or keys."""
    import re
    from urllib.parse import urlparse
    for h in list(r.history) + [r]:
        u = urlparse(h.url)
        print(f"  hop: HTTP {h.status_code} {u.netloc}{u.path[:40]} type={h.headers.get('Content-Type', '?')[:40]}")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", r.text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    print("  page text:", text[:300] or "(none)")


def _selftest() -> int:
    """python sheets_sink.py  -> posts one row to the Status tab and prints what Google answered."""
    import datetime as dt
    import os
    url, secret = os.getenv("SHEETS_WEBHOOK_URL"), os.getenv("SHEETS_SECRET")
    print("SHEETS_WEBHOOK_URL set:", bool(url), "| SHEETS_SECRET set:", bool(secret))
    if not (url and secret):
        print("Fill both in .env, then run: docker compose up -d")
        return 1
    try:
        r = requests.post(url, json={"secret": secret, "tab": "Status", "header": STATUS_HEADER,
                                     "rows": [[dt.datetime.now().isoformat(timespec="seconds"), "test",
                                               "SELFTEST", "connection check"]]}, timeout=30)
    except requests.RequestException as exc:
        print("Could not reach the URL:", exc.__class__.__name__)
        return 1
    body = r.text.strip()
    print("HTTP", r.status_code, "| reply:", body[:80].replace("\n", " "))
    if body != "ok":
        _explain(r)
    if body == "ok":
        print("OK: check your sheet for a 'Status' tab.")
        return 0
    if body == "forbidden":
        print("The secret in .env differs from SECRET in the Apps Script.")
    elif "<html" in body.lower() or r.status_code in (302, 401, 403, 404):
        print("Google returned a page, not 'ok': the web app is not deployed with 'Who has access: Anyone', "
              "or the URL is wrong/old. Redeploy and copy the /exec URL.")
    return 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
