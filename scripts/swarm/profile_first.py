"""The profile a task's agent runs, decided by the tick before placement chooses where that agent runs."""

import json
import os
from contextlib import suppress

from scripts.swarm import live_binding, profile_choice, seat_spawn
from scripts.swarm.store import RedisStore, SwarmConfig

KEY = "profile-decisions"
FIELD = "profile_decision"


def placed(runtime: object) -> bool:
    return getattr(getattr(runtime, "router", None), "placement", None) is not None


def placeable(
    slug: str, config: SwarmConfig, store: RedisStore, runtime: object, lane: str, ready: list[dict], room: int
) -> list[dict]:
    """The first ready tasks this hive places, at most room; a task is decided only when a free slot reaches it."""
    found = []
    for task in ready:
        if len(found) == room:
            break
        decided(slug, config, store, runtime, lane, task)
        if not seat_spawn.placed_elsewhere(runtime, lane, task):
            found.append(task)
    return found


def decided(slug: str, config: SwarmConfig, store: RedisStore, runtime: object, lane: str, task: dict) -> dict:
    if placed(runtime):
        # an undecided task keeps its own profile for placement and is decided again by its spawn, which settles errors
        with suppress(Exception):
            task[FIELD] = _decision(slug, config, store, lane, task)
    return task


def _decision(slug: str, config: SwarmConfig, store: RedisStore, lane: str, task: dict) -> dict:
    assignment = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    relaunch = live_binding.complete(json.loads(assignment) if assignment else None)
    saved = relaunch or (store.handoff_envelope(slug, task["id"]) or {}).get("launch") or {}
    if kept := profile_choice.kept(task, saved, relaunch):
        return kept.record()
    if not task.get("profile") and (cached := store.redis.hget(store.key(slug, KEY), task["id"])):
        return json.loads(cached)
    decision = profile_choice.pick(slug, lane, config.lanes.get(lane, {}), task, dict(os.environ))
    if decision.model:
        store.redis.hset(store.key(slug, KEY), task["id"], json.dumps(decision.record()))
    return decision.record()


def spawned(slug: str, store: RedisStore, task_id: str) -> None:
    store.redis.hdel(store.key(slug, KEY), task_id)
