"""The live sub-agents of each session: SubagentStart records one, SubagentStop clears it, and a record older than
its time to live counts as gone, so a helper whose stop never fired cannot hold a session forever."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TTL_S = 3600
SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe(name: str) -> str:
    """The name as one plain path component, never a dot entry."""
    plain = SAFE.sub("_", name)
    return "_" if plain in {".", ".."} else plain


def folder(session: str, home: Path | None = None) -> Path:
    """The folder holding the session's live helper records."""
    if home is None:
        from hooks.config import AGENTIHOOKS_HOME

        home = Path(AGENTIHOOKS_HOME)
    return home / "helpers" / safe(session)


def fresh(record: Path, moment: float) -> bool:
    """Whether the record is younger than the time to live; one cleared while it is read counts as gone."""
    try:
        return moment - record.stat().st_mtime < TTL_S
    except FileNotFoundError:
        return False


def start(session: str, agent: str, home: Path | None = None) -> None:
    """Record a helper of the session as live."""
    if not (session and agent) or session == agent:
        return
    path = folder(session, home)
    path.mkdir(parents=True, exist_ok=True)
    (path / safe(agent)).touch()


def stop(session: str, agent: str, home: Path | None = None) -> None:
    """Clear the helper's record."""
    if session and agent:
        (folder(session, home) / safe(agent)).unlink(missing_ok=True)


def track(payload: Mapping[str, Any], started: bool, home: Path | None = None) -> None:
    """Record or clear the helper a SubagentStart or SubagentStop payload names under its parent session."""
    session, agent = str(payload.get("session_id") or ""), str(payload.get("agent_id") or "")
    (start if started else stop)(session, agent, home)


def live(session: str, home: Path | None = None, now: float | None = None) -> bool:
    """Whether a helper of the session started within the time to live and has not stopped."""
    path = folder(session, home) if session else None
    if path is None or not path.is_dir():
        return False
    moment = time.time() if now is None else now
    return any(fresh(record, moment) for record in path.iterdir())
