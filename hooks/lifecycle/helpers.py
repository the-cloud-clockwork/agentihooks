"""The live sub-agents of each session: SubagentStart records one, SubagentStop clears it, and a record older than
its time to live counts as gone, so a helper whose stop never fired cannot hold a session forever."""

from __future__ import annotations

import re
import time
from pathlib import Path

from hooks.lifecycle.guard import _home

TTL_S = 3600
SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def folder(session: str, home: Path | None = None) -> Path:
    """The folder holding the session's live helper records."""
    return (home or _home()) / "helpers" / SAFE.sub("_", session)


def start(session: str, agent: str, home: Path | None = None) -> None:
    """Record a helper of the session as live."""
    if not (session and agent) or session == agent:
        return
    path = folder(session, home)
    path.mkdir(parents=True, exist_ok=True)
    (path / SAFE.sub("_", agent)).touch()


def stop(session: str, agent: str, home: Path | None = None) -> None:
    """Clear the helper's record."""
    if session and agent:
        (folder(session, home) / SAFE.sub("_", agent)).unlink(missing_ok=True)


def live(session: str, home: Path | None = None, now: float | None = None) -> bool:
    """Whether a helper of the session started within the time to live and has not stopped."""
    path = folder(session, home) if session else None
    if path is None or not path.is_dir():
        return False
    moment = time.time() if now is None else now
    return any(moment - record.stat().st_mtime < TTL_S for record in path.iterdir())
