from __future__ import annotations

from scripts.swarm.store import RedisStore


def _key(store: RedisStore, slug: str) -> str:
    return store.key(slug, "deliberate-exits")


def record(store: RedisStore, slug: str, name: str, at: int) -> None:
    store.redis.hset(_key(store, slug), name, str(at))


def recorded(store: RedisStore, slug: str, name: str) -> bool:
    return bool(store.redis.hexists(_key(store, slug), name))


def take(store: RedisStore, slug: str, name: str) -> bool:
    return bool(store.redis.hdel(_key(store, slug), name))
