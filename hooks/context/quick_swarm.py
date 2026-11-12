"""The quick swarm block a quick swarm member receives, on the cadence of the swarm refocus."""

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

EVERY = 40
STATE_DIR = "quick_refocus"


def _state_path(environ: Mapping[str, str], session_id: str) -> Path:
    home = environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME") or Path.home()) / ".agentihooks"
    return Path(home) / STATE_DIR / f"{session_id}.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    try:
        items: Any = data.items()
    except AttributeError:
        return {}
    return {str(key): value for key, value in items}


def refocus(session_id: str, event: str, environ: Mapping[str, str] | None = None) -> str:
    """The quick swarm block when it is due: first sight, after compaction, on change and every EVERY tool calls."""
    from scripts.quick import member, seeds

    env = os.environ if environ is None else environ
    found = member.find(env) if session_id else None
    if found is None:
        return ""
    block = seeds.block(found)
    digest = hashlib.sha256(block.encode()).hexdigest()
    path = _state_path(env, session_id)
    state = _load(path)
    calls = int(state.get("calls", 0)) + (event == "tool")
    due = (
        event == "compact"
        or state.get("hash") != digest
        or (event == "tool" and calls - int(state.get("at", 0)) >= EVERY)
    )
    state = {"hash": digest, "calls": calls, "at": calls if due else int(state.get("at", 0))}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return block if due else ""
