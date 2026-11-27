import json
from typing import Any

from scripts.swarm import lifecycle
from scripts.swarm.store import RedisStore


def view(store: RedisStore, slug: str) -> dict[str, Any]:
    status = json.loads(store.redis.get(store.key(slug, "published")) or "{}").get("status", {})
    # ruleid: swarm-view-reads-published-twice
    return {**status, "lifecycle": lifecycle.view(store, slug)}


def view_once(store: RedisStore, slug: str) -> dict[str, Any]:
    status = json.loads(store.redis.get(store.key(slug, "published")) or "{}").get("status", {})
    # ok: swarm-view-reads-published-twice
    return {**status, "lifecycle": lifecycle.view(store, slug, task_revisions=status.get("task_revisions") or {})}


def outside_the_rule_scope(store: RedisStore, slug: str) -> dict[str, Any]:
    # ok: swarm-view-reads-published-twice
    return lifecycle.view(store, slug)
