"""A handoff successor whose routed spawn is refused gets one more launch steered off that harness; a second failure,
or no start within the first attempt's launch window, falls through to the tick's reopen."""

import json
from dataclasses import asdict, replace
from pathlib import Path

from scripts.swarm import failed_panes

RETRIED = "successor_retried"


def due(task: dict, primed: dict, error) -> str:
    from scripts.swarm.tick import SpawnError

    if not task.get("handoff") or primed.get(RETRIED) or not isinstance(error, SpawnError):
        return ""
    return error.harness if error.status == "refused" else ""


def steered(primed: dict, harness: str) -> dict:
    task = {**primed, "avoid_harness": harness, RETRIED: True}
    if task.get("transfer"):
        task["transfer"] = {**task["transfer"], "avoid_harness": harness}
    return task


def note(slug: str, store, runtime, record, error, now_ms: int) -> str:
    failed_panes.hold(store, slug, replace(record, pane_id=error.pane_id), now_ms)
    if reap := getattr(runtime, "reap_name", None):
        reap(record.name)
    store.note_launch_failure(slug, record.task, str(error), error.harness)
    return f"retrying {record.name} for {record.task} away from {error.harness} after: {error}"


def relaunch(run, agent, saved: dict, error) -> tuple[str, Exception | None]:
    from scripts.swarm import launch_job
    from scripts.swarm.tick import SpawnError

    primed = saved.get("primed") or {}
    harness = due(saved["task"], primed, error) if primed else ""
    if not harness:
        return "", error
    picked = saved["placed"]["launch_job"]
    Path(picked["path"]).unlink(missing_ok=True)
    said = note(run.slug, run.store, run.runtime, agent, error, run.now_ms)
    retried = steered(primed, harness)
    try:
        placed = run.runtime.spawn(run.config, agent.lane, agent.name, retried)
        if not placed.launch_job:
            raise SpawnError("a retried successor launch ran outside a workstation job", harness=placed.harness)
        kept = replace(placed, launch_job={**placed.launch_job, "started_at": picked["started_at"]})
        entry = {"task": saved["task"], "primed": retried, "placed": asdict(kept)}
        run.store.redis.hset(run.store.key(run.slug, launch_job.KEY), agent.name, json.dumps(entry))
        launch_job.spawn(placed.launch_job)
    except Exception as exc:
        return "", exc if isinstance(exc, SpawnError) else SpawnError(str(exc))
    return said, None
