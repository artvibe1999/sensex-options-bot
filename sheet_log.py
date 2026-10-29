"""Mirror the bot's important log lines to a "Log" tab of the Google Sheet, so the bot can be checked without
shell access to the VM.

What is mirrored: WARNING and above, plus the key INFO events (entries, exits, entry-filter passes, exit-engine
floor / arm / stop / adaptation lines, new day, risk profile). Entry-filter REJECTIONS are summarised once an hour
instead of line by line. Secrets are redacted before anything leaves the process.

Design: emit() only appends to a bounded in-memory list (no I/O, never blocks or raises); a daemon thread flushes
a batch every `flush_s` through the existing SheetsSink queue. A busy burst is capped at `max_rows` per flush.
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import os
import re
import subprocess
import threading
from typing import Optional

from config import IST

LOG_HEADER = ["Time (IST)", "Mode", "Level", "Source", "Message"]
KEEP_INFO = ("ENTRY-FILTER pass", "EXIT-ENGINE", "ENTRY ", "EXIT ", "New trading day", "Risk profile",
             "Bot started", "[PAPER]")
SKIP_LOGGERS = ("sheets", "urllib3", "requests", "alerts")      # never mirror the senders themselves (no feedback loop)

_REDACT = [
    (re.compile(r"(?i)(access_token|request_token|api_key|api_secret|secret|password|token|state|checksum)"
                r"(=|%3[dD]|\"?\s*:\s*\"?)[^\s&\"',}]+"), r"\1\2[redacted]"),
    (re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"), "[redacted-bot-token]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "[redacted-long-string]"),
]


def redact(text: str) -> str:
    for rx, sub in _REDACT:
        text = rx.sub(sub, text)
    return text


def build_info() -> str:
    """Short identity of the running build: git commit if available, else the code's modification time."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return f"commit {out.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        ts = dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(here, "main.py")), IST)
        return f"code files dated {ts:%Y-%m-%d %H:%M}"
    except OSError:
        return "unknown build"


class SheetLogHandler(logging.Handler):
    def __init__(self, sink, mode: str, flush_s: float = 60.0, max_rows: int = 40, summary_s: float = 3600.0) -> None:
        super().__init__(level=logging.INFO)
        self.sink, self.mode = sink, mode
        self.flush_s, self.max_rows, self.summary_s = flush_s, max_rows, summary_s
        self._rows: collections.deque = collections.deque(maxlen=500)
        self._rejects: collections.Counter = collections.Counter()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_summary = 0.0
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ input
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(SKIP_LOGGERS):
                return
            msg = record.getMessage()
            if record.levelno < logging.WARNING:
                if msg.startswith("ENTRY-FILTER reject"):
                    # "ENTRY-FILTER reject <variant>: direction: ...; skew: ..." -> count by the first failed check
                    why = msg.split(": ", 1)[1] if ": " in msg else "?"
                    with self._lock:
                        self._rejects[why.split(":", 1)[0].strip()[:20]] += 1
                    return
                if not msg.startswith(KEEP_INFO):
                    return
            ts = dt.datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S")
            row = [ts, self.mode, record.levelname, record.name, redact(msg)[:300]]
            with self._lock:
                self._rows.append(row)
        except Exception:  # noqa: BLE001 - logging must never break the bot
            self.handleError(record)

    # ------------------------------------------------------------------ output
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sheet-log")
        self._thread.start()
        self.note("Bot started: " + build_info())

    def note(self, text: str) -> None:
        ts = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._rows.append([ts, self.mode, "INFO", "bot", redact(text)[:300]])

    def _loop(self) -> None:
        while not self._stop.wait(self.flush_s):
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                logging.getLogger("sheet_log").exception("Log flush failed")

    def flush(self, now_s: Optional[float] = None) -> int:
        """Send queued rows (deduplicating consecutive repeats, capped). Returns rows handed to the sink."""
        import time
        now_s = time.monotonic() if now_s is None else now_s
        with self._lock:
            rows = list(self._rows)
            self._rows.clear()
            if self._rejects and now_s - self._last_summary >= self.summary_s:
                top = ", ".join(f"{k} x{v}" for k, v in self._rejects.most_common(4))
                rows.append([dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), self.mode, "INFO", "entry_filter",
                             f"entry-filter rejections since last summary: {sum(self._rejects.values())} ({top})"])
                self._rejects.clear()
                self._last_summary = now_s
            elif not self._last_summary:
                self._last_summary = now_s
        out: list = []
        for r in rows:
            if out and out[-1][2:] == r[2:]:
                continue                                   # identical consecutive line
            out.append(r)
        if len(out) > self.max_rows:
            dropped = len(out) - self.max_rows
            out = out[-self.max_rows:]
            out.insert(0, [out[0][0], self.mode, "WARNING", "sheet_log", f"{dropped} earlier log lines omitted"])
        if out:
            self.sink.append("Log", LOG_HEADER, out)
        return len(out)

    def close(self) -> None:
        self._stop.set()
        super().close()
