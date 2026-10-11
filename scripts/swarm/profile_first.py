"""The profile a task's agent runs, decided by the tick before placement chooses where that agent runs."""

import json
import os

from scripts.swarm import profile_choice

KEY = "profile-decisions"
FIELD = "profile_decision"


def placed(runtime) -> bool:
    return getattr(getattr(runtime, "router", None), "placement", None) is not None


def decided(slug: str, config, store, runtime, lane: str, task: dict) -> dict:
    """Records the decision on the task; a model answer is kept until the task spawns so it is asked once."""
    if task.get("profile") or not placed(runtime):
        return task
    saved = store.redis.hget(store.key(slug, KEY), task["id"])
    if saved:
        task[FIELD] = json.loads(saved)
        return task
    try:
        decision = profile_choice.pick(slug, lane, config.lanes.get(lane, {}), task, dict(os.environ))
    except profile_choice.ProfileUnresolved:
        return task
    task[FIELD] = decision.record()
    if decision.model:
        store.redis.hset(store.key(slug, KEY), task["id"], json.dumps(task[FIELD]))
    return task


def spawned(slug: str, store, task_id: str) -> None:
    store.redis.hdel(store.key(slug, KEY), task_id)
