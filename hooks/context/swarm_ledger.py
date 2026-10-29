"""Runs the swarm ledger hook inside the shared hooks entry, so every harness reaches it."""

import io
import json
from contextlib import redirect_stdout

from hooks.context import worker_mode


def run(payload: dict) -> tuple[list[str], str | None]:
    """Dispatch one event to the ledger hook; return its context blocks and its stop block reason."""
    from scripts.swarm_ledger import ledger_hook

    if worker_mode.active():
        return [], None
    out = io.StringIO()
    with redirect_stdout(out):
        ledger_hook.dispatch(payload)
    contexts, block = [], None
    for line in out.getvalue().splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("decision") == "block":
            block = doc.get("reason")
        text = (doc.get("hookSpecificOutput") or {}).get("additionalContext")
        if text:
            contexts.append(text)
    return contexts, block
