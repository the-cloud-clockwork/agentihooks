"""The quick swarm block a quick swarm member receives, on the cadence of the swarm refocus."""

import hashlib
import json
import os
from collections.abc import Mapping

EVERY = 40
STATE_DIR = "quick_refocus"


def refocus(session_id: str, event: str, environ: Mapping[str, str] | None = None) -> str:
    """The quick swarm block when it is due: first sight, after compaction, on change and every EVERY tool calls."""
    from scripts.quick import member, paths, seeds

    env = os.environ if environ is None else environ
    found = member.find(env) if session_id else None
    if found is None:
        return ""
    block = seeds.block(found)
    digest = hashlib.sha256(block.encode()).hexdigest()
    path = paths.home(env) / STATE_DIR / f"{session_id}.json"
    state = paths.read_object(path)
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
