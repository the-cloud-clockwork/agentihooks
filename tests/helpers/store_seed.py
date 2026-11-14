"""Seeds the swarm store rows the removed legacy tick launch wrote, for tests that read them through live code."""

import json

from scripts.swarm.store import AgentRecord, RedisStore


def count_spawn(store: RedisStore, slug: str, harness: str) -> None:
    """Counts one launch of the harness in the swarm's spawn tally."""
    store.redis.hincrby(store.key(slug, "spawns"), harness or "unknown", 1)


def count_claim(store: RedisStore, slug: str, task: str) -> int:
    """Counts one started life of the task and returns the lives counted so far."""
    started = sum(row["task"] == task and row["state"] == "started" for row in store.launches(slug, task))
    store.redis.hdel(store.key(slug, "pending-lives"), task)
    store.redis.hsetnx(store.key(slug, "started-lives"), task, started)
    return int(store.redis.hincrby(store.key(slug, "started-lives"), task))


def record_launch(
    store: RedisStore,
    slug: str,
    agent: AgentRecord,
    state: str,
    error: str = "",
    after: str = "",
    head: str | None = "",
) -> None:
    """Writes the agent's launch row in the given state."""
    row = {
        "agent": agent.name,
        "task": agent.task,
        "at": agent.started_at,
        "state": state,
        "error": error,
        "pane_id": agent.pane_id,
        "after": after,
        "head": head,
    }
    store.redis.hset(store.key(slug, "launches"), agent.name, json.dumps(row))


def take_restart(store: RedisStore, slug: str, task: str) -> str:
    """Reads and clears the restart cause marked for the task."""
    cause = str(store.redis.hget(store.key(slug, "restart-causes"), task) or "")
    store.redis.hdel(store.key(slug, "restart-causes"), task)
    return cause
