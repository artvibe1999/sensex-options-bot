"""The user's daily rule: after the first trade that CLOSES WITH A NET PROFIT (after charges), no new entries for the
rest of the day. A losing trade does not lock; the risk manager's daily loss limit and max trades still apply.

The lock blocks entries only: an open position is always managed to its exit. It is persisted with the bot's state,
so a restart cannot unlock the day. Checked in the main loop's thread right before an entry is placed (the only thread
that places orders), so no second entry can race past it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional


@dataclass
class DayGuard:
    enabled: bool = True
    day: Optional[dt.date] = None
    locked: bool = False
    reason: str = ""
    closed: int = 0
    wins: int = 0

    def roll(self, today: dt.date) -> None:
        if self.day != today:
            self.day, self.locked, self.reason, self.closed, self.wins = today, False, "", 0, 0

    def allows_entry(self, today: dt.date) -> bool:
        self.roll(today)
        return not (self.enabled and self.locked)

    def on_trade_closed(self, today: dt.date, net: float) -> bool:
        """Record a fully closed trade. Returns True when this trade locked the day."""
        self.roll(today)
        self.closed += 1
        if net > 0:
            self.wins += 1
            if self.enabled and not self.locked:
                self.locked, self.reason = True, f"first profitable trade of the day closed (+{net:,.0f})"
                return True
        return False

    def export(self) -> dict:
        return {"enabled": self.enabled, "day": self.day.isoformat() if self.day else None, "locked": self.locked,
                "reason": self.reason, "closed": self.closed, "wins": self.wins}

    @classmethod
    def restore(cls, d: dict, enabled: bool = True) -> "DayGuard":
        g = cls(enabled=enabled)
        try:
            g.day = dt.date.fromisoformat(d["day"]) if d.get("day") else None
            g.locked, g.reason = bool(d.get("locked", False)), str(d.get("reason", ""))
            g.closed, g.wins = int(d.get("closed", 0)), int(d.get("wins", 0))
        except (KeyError, TypeError, ValueError):
            return cls(enabled=enabled)
        return g
