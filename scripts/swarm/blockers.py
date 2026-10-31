"""Blocker queue: errors and stalls recorded per swarm and category, aged out after 24 hours.

sustained() is what the master coding window and its tick-pass notice read: a category stuck for an hour or more,
still recurring in the last RECENT_MS, blocks other work until it is cleared.
"""

import json
from dataclasses import dataclass
from typing import Any

from scripts.swarm.store import PREFIX, RedisStore

TTL_MS = 24 * 60 * 60 * 1000
SUSTAINED_MS = 60 * 60 * 1000
RECENT_MS = 15 * 60 * 1000
NOTICE_MS = 60 * 1000
KEY = "blockers"
NOTICE_KEY = "blockers-notice"


@dataclass(frozen=True)
class Blocker:
    category: str
    first_seen: int
    last_seen: int
    count: int
    detail: str


def _key(slug: str) -> str:
    return f"{PREFIX}:{slug}:{KEY}"


def _client(store: RedisStore) -> Any:
    return getattr(store, "redis")


def record(store: RedisStore, slug: str, category: str, detail: str, now_ms: int) -> None:
    key = _key(slug)
    raw = _client(store).hget(key, category)
    previous = json.loads(raw) if raw else None
    row = {
        "first_seen": previous["first_seen"] if previous else now_ms,
        "last_seen": now_ms,
        "count": (previous["count"] + 1) if previous else 1,
        "detail": detail,
    }
    _client(store).hset(key, category, json.dumps(row))
    _client(store).pexpire(key, TTL_MS)


def clear(store: RedisStore, slug: str, category: str) -> None:
    _client(store).hdel(_key(slug), category)


def active(store: RedisStore, slug: str, now_ms: int) -> dict[str, Blocker]:
    key = _key(slug)
    found: dict[str, Blocker] = {}
    for category, raw in _client(store).hgetall(key).items():
        row = json.loads(raw)
        if now_ms - row["first_seen"] >= TTL_MS:
            _client(store).hdel(key, category)
            continue
        found[category] = Blocker(category, row["first_seen"], row["last_seen"], row["count"], row["detail"])
    return found


def sustained(store: RedisStore, slug: str, now_ms: int) -> dict[str, Blocker]:
    return {
        category: blocker
        for category, blocker in active(store, slug, now_ms).items()
        if blocker.last_seen - blocker.first_seen >= SUSTAINED_MS and now_ms - blocker.last_seen <= RECENT_MS
    }


def due_notice(store: RedisStore, slug: str, now_ms: int) -> bool:
    key = f"{PREFIX}:{slug}:{NOTICE_KEY}"
    last = _client(store).get(key)
    if last is not None and now_ms - int(last) < NOTICE_MS:
        return False
    _client(store).set(key, now_ms)
    return True
