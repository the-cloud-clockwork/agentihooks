"""When each swarm agent entered the input prompt it waits on, kept beside its row so the row stays readable by older
strict readers."""

from typing import Any

from scripts.swarm import elapsed

KIND = "input-since"


def _key(store, slug, name):
    return store.key(slug, KIND, name)


def mark(store: Any, slug: str, name: str, now_ms: int, counted: int) -> int:
    """When the agent began waiting on its prompt: kept while ``counted`` passes run on, else anchored to them."""
    return elapsed.start(store, slug, KIND, name, now_ms, counted)


def clear(store, slug: str, name: str) -> None:
    store.redis.delete(_key(store, slug, name))


def since(store, slug: str, name: str) -> int | None:
    raw = store.redis.get(_key(store, slug, name))
    return None if raw is None else int(raw)
