"""Per-session counts of telemetry signals queued, dropped, unsupported, unconfirmed, accepted or failed."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

UNATTRIBUTED = "unattributed"
PREFIX = "agentihooks.signals."


def safe_name(session_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or UNATTRIBUTED


def _path(session_id: str) -> Path:
    from hooks import config

    return config.AGENTIHOOKS_HOME / "telemetry" / "signals" / f"{safe_name(session_id)}.json"


def record(counts: Mapping[tuple[str, str, str], int]) -> None:
    """Adds (session, signal, outcome) counts to each session's file."""
    sessions: dict[str, dict[str, int]] = {}
    for (session, signal, outcome), count in counts.items():
        if count:
            keys = sessions.setdefault(safe_name(session), {})
            keys[f"{signal}.{outcome}"] = keys.get(f"{signal}.{outcome}", 0) + count
    for session, added in sessions.items():
        path = _path(session)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            folder = os.open(path.parent, os.O_RDONLY)
            try:
                fcntl.flock(folder, fcntl.LOCK_EX)
                totals = read(session)
                for key, count in added.items():
                    totals[key] = totals.get(key, 0) + count
                handle, temp = tempfile.mkstemp(dir=path.parent)
                with os.fdopen(handle, "w") as out:
                    json.dump(totals, out)
                os.replace(temp, path)
            finally:
                os.close(folder)
        except OSError:
            continue


def read(session_id: str) -> dict[str, int]:
    try:
        data = json.loads(_path(session_id).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


def attributes(session_id: str) -> dict[str, int]:
    return {f"{PREFIX}{key}": count for key, count in sorted(read(session_id).items())}
