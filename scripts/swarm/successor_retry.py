"""A handoff successor whose routed spawn is refused gets one more launch steered off that harness; a second failure,
or no start within the first attempt's launch window, falls through to the tick's reopen."""

from dataclasses import replace
from typing import TYPE_CHECKING

from scripts.swarm import failed_panes
from scripts.swarm.store import AgentRecord, RedisStore

if TYPE_CHECKING:
    from scripts.swarm.tick import Runtime, SpawnError

RETRIED = "successor_retried"


def due(task: dict, primed: dict, error: Exception | None) -> str:
    from scripts.swarm.tick import SpawnError

    if not task.get("handoff") or primed.get(RETRIED) or not isinstance(error, SpawnError):
        return ""
    return error.harness if error.status == "refused" else ""


def steered(primed: dict, harness: str) -> dict:
    task = {**primed, "avoid_harness": harness, RETRIED: True}
    if task.get("transfer"):
        task["transfer"] = {**task["transfer"], "avoid_harness": harness}
    return task


def note(
    slug: str, store: RedisStore, runtime: "Runtime", record: AgentRecord, error: "SpawnError", now_ms: int
) -> str:
    failed_panes.hold(store, slug, replace(record, pane_id=error.pane_id), now_ms)
    runtime.reap_name(record.name)
    store.note_launch_failure(slug, record.task, str(error), error.harness)
    return f"retrying {record.name} for {record.task} away from {error.harness} after: {error}"
