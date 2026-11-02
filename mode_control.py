"""Which mode the bot runs in: data/mode.json, written by a confirmed Telegram /mode switch, read at start-up.

PAPER unless the file says LIVE **and** the environment's master switch LIVE_TRADING_CONFIRM=YES is set; a LIVE
request without the master switch starts PAPER and says why. The file is written atomically; anything unreadable or
unknown means PAPER (fail safe).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from typing import Mapping, Optional

MODES = ("PAPER", "LIVE")


def live_allowed(env: Mapping[str, str]) -> bool:
    return env.get("LIVE_TRADING_CONFIRM") == "YES"


def read_mode(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            m = str(json.load(f).get("mode", "PAPER")).upper()
    except (OSError, ValueError, AttributeError):
        return "PAPER"
    return m if m in MODES else "PAPER"


def write_mode(path: str, mode: str, by: str = "telegram") -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".mode-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "by": by, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}, f)
    os.replace(tmp, path)


def startup_mode(path: str, env: Mapping[str, str], cli: Optional[str] = None) -> tuple[str, str]:
    """(mode to run, note). `cli` is "PAPER" / "LIVE" from an explicit --paper / --live flag, else None (--auto)."""
    want = cli or read_mode(path)
    if want == "LIVE" and not live_allowed(env):
        return "PAPER", "LIVE was requested but LIVE_TRADING_CONFIRM is not YES in .env: running PAPER"
    return want, ""
