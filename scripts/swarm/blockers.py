"""Blocker queue: errors and stalls recorded per swarm and category, aged out after 24 hours.

sustained() is what the master coding window and its tick-pass notice read: a category stuck for an hour or more,
still recurring in the last RECENT_MS, blocks other work until it is cleared.
"""

import json
from dataclasses import dataclass

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


def _key(store, slug):
    return store.key(slug, KEY)


def record(store, slug, category, detail, now_ms):
    key = _key(store, slug)
    raw = store.redis.hget(key, category)
    previous = json.loads(raw) if raw else None
    row = {
        "first_seen": previous["first_seen"] if previous else now_ms,
        "last_seen": now_ms,
        "count": (previous["count"] + 1) if previous else 1,
        "detail": detail,
    }
    store.redis.hset(key, category, json.dumps(row))
    store.redis.pexpire(key, TTL_MS)


def clear(store, slug, category):
    store.redis.hdel(_key(store, slug), category)


def active(store, slug, now_ms):
    key = _key(store, slug)
    found = {}
    for category, raw in store.redis.hgetall(key).items():
        row = json.loads(raw)
        if now_ms - row["first_seen"] >= TTL_MS:
            store.redis.hdel(key, category)
            continue
        found[category] = Blocker(category, row["first_seen"], row["last_seen"], row["count"], row["detail"])
    return found


def sustained(store, slug, now_ms):
    return {
        category: blocker
        for category, blocker in active(store, slug, now_ms).items()
        if blocker.last_seen - blocker.first_seen >= SUSTAINED_MS and now_ms - blocker.last_seen <= RECENT_MS
    }


def due_notice(store, slug, now_ms):
    key = store.key(slug, NOTICE_KEY)
    last = store.redis.get(key)
    if last is not None and now_ms - int(last) < NOTICE_MS:
        return False
    store.redis.set(key, now_ms)
    return True
