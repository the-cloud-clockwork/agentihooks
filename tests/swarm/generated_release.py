"""The release task the phase builder generates, in the shape the ledger stores it."""

from types import SimpleNamespace
from typing import Any

from scripts.swarm import phase_planning


def generated_release(pid: str = "p1", title: str = "Build") -> dict[str, Any]:
    """The open task phase_planning._add_release adds for phase pid, keyed by id as the ledger task add stores it."""
    added: list[dict[str, Any]] = []
    ledger = SimpleNamespace(add_task=lambda slug, task, by: added.append(dict(task)))
    phase_planning._add_release("demo", {"id": pid, "title": title}, ledger)
    [built] = added
    return {"id": built.pop("task"), "state": "open", **built}
