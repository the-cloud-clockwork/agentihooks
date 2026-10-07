"""Per-session counts of telemetry signals queued, dropped, unsupported, unconfirmed, accepted or failed."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Mapping
from pathlib import Path

UNATTRIBUTED = "unattributed"
PREFIX = "agentihooks.signals."


def _path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id or UNATTRIBUTED)
    from hooks import config

    return config.AGENTIHOOKS_HOME / "telemetry" / "signals" / f"{safe}.json"


def record(counts: Mapping[tuple[str, str, str], int]) -> None:
    """Adds (session, signal, outcome) counts to each session's file."""
    sessions: dict[str, dict[str, int]] = {}
    for (session, signal, outcome), count in counts.items():
        if count:
            keys = sessions.setdefault(session or UNATTRIBUTED, {})
            keys[f"{signal}.{outcome}"] = keys.get(f"{signal}.{outcome}", 0) + count
    for session, added in sessions.items():
        path = _path(session)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(".lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                totals = read(session)
                for key, count in added.items():
                    totals[key] = totals.get(key, 0) + count
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(totals), encoding="utf-8")
                temp.replace(path)
        except OSError:
            continue


def read(session_id: str) -> dict[str, int]:
    try:
        data = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


def attributes(session_id: str) -> dict[str, int]:
    return {f"{PREFIX}{key}": count for key, count in sorted(read(session_id).items())}
