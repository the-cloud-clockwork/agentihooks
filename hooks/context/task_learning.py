"""Records the lesson and decision markers a swarm task session wrote as learning outcomes on its task."""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

KINDS = ("lesson", "decision")

Json = dict[str, Any]


class Ledger(Protocol):
    """The ledger reads and writes the recorder needs."""

    def task(self, slug: str, task_id: str) -> Json:
        """Return one task row."""
        ...

    def record_learning(self, slug: str, ops: list[Json]) -> None:
        """Submit brain learning operations."""
        ...


def operations(markers: list[Json], task: Json, seat: str, by: str, session: str) -> list[Json]:
    """Return one brain learning operation per lesson or decision marker, or none when the task cannot hold one."""
    from scripts.swarm_ledger import ledger_brain

    if not task.get("proof") or not task.get("brain_policy", {}).get("brain"):
        return []
    scope = ledger_brain.current(task)
    ops: list[Json] = []
    for marker in markers:
        learning = {
            "kind": marker["type"],
            "content": marker["content"][:4096],
            "sources": [f"session:{session}"[:1000]],
            "seat": seat,
        }
        body = {
            "op": "brain_learning",
            "item": f"tasks/{task['id']}",
            "by": by,
            "binding": scope,
            "learning": learning,
            "proof": task["proof"],
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
        ops.append({**body, "id": f"learning-{digest}"})
    return ops


def default_seat(name: str) -> str:
    """Return the seat the named agent occupies, from the inbox seat registry."""
    from scripts.inbox.bus import seat_of
    from scripts.inbox.store import connect

    return seat_of(connect(), name)


def record(
    session: str,
    transcript: str,
    environ: Mapping[str, str] | None = None,
    ledger: Ledger | None = None,
    seat: Callable[[str], str] | None = None,
) -> int:
    """Record the session's lesson and decision markers on its swarm task and return how many were submitted."""
    from hooks.config import BRAIN_WRITER_MAX_MARKERS
    from hooks.context.brain_writer_hook import _parse_transcript_for_markers

    env = os.environ if environ is None else environ
    slug, task_id, by = (env.get(key) for key in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"))
    if not (slug and task_id and by):
        return 0
    markers = [m for m in _parse_transcript_for_markers(transcript, BRAIN_WRITER_MAX_MARKERS) if m["type"] in KINDS]
    if not markers:
        return 0
    if ledger is None:
        from scripts.swarm.ledger_client import LedgerClient

        ledger = LedgerClient()
    ops = operations(markers, ledger.task(slug, task_id), (seat or default_seat)(by), by, session)
    if ops:
        ledger.record_learning(slug, ops)
    return len(ops)
