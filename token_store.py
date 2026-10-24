"""Shared reader/writer for the daily Kite access token file.

The token file is written by ``token_service.py`` (after you complete the
daily Zerodha login) and read by the bot. Writes are atomic and the file is
chmod 600. Kite access tokens expire around 06:00 IST every day.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_HOUR_IST = 6


def read_token(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) and rec.get("access_token") else None
    except (OSError, ValueError):
        return None


def write_token(path: str, access_token: str, user_id: str = "") -> dict:
    rec = {"access_token": access_token, "user_id": user_id,
           "issued_at": dt.datetime.now(IST).isoformat()}
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rec


def _last_expiry_cutoff(now: dt.datetime) -> dt.datetime:
    cutoff = now.replace(hour=EXPIRY_HOUR_IST, minute=0, second=0, microsecond=0)
    return cutoff if now >= cutoff else cutoff - dt.timedelta(days=1)


def is_fresh(rec: Optional[dict], now: Optional[dt.datetime] = None) -> bool:
    """True if the token was issued after the most recent 06:00 IST expiry."""
    if not rec:
        return False
    now = now or dt.datetime.now(IST)
    try:
        issued = dt.datetime.fromisoformat(rec["issued_at"])
    except (KeyError, ValueError):
        return False
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=IST)
    return issued >= _last_expiry_cutoff(now)
