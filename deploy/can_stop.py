"""Decide whether the VM may be powered off. Standard library only (runs on the VM host, not in Docker).

    python3 deploy/can_stop.py [path/to/state.json]

Exit code 0 = safe to stop; 1 = an open position is recorded; 2 = the state file is unreadable (fail closed).
A missing state file means the bot never traded here, so stopping is safe.
"""
from __future__ import annotations

import json
import sys


def check(path: str) -> tuple[int, str]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return 0, "no state file: nothing open"
    except (OSError, ValueError) as exc:
        return 2, f"state file unreadable ({exc.__class__.__name__}): not stopping"
    pos = doc.get("position") if isinstance(doc, dict) else None
    if pos:
        name = (pos.get("plan") or {}).get("name", "?") if isinstance(pos, dict) else "?"
        return 1, f"open position ({name}, mode {doc.get('mode')}): not stopping"
    return 0, "no open position"


if __name__ == "__main__":
    code, msg = check(sys.argv[1] if len(sys.argv) > 1 else "data/state.json")
    print(msg)
    raise SystemExit(code)
