"""When each swarm agent entered the input prompt it waits on, kept beside its row so the row stays readable by older
strict readers."""

DAY_S = 86_400


def _key(store, slug, name):
    return store.key(slug, "input-since", name)


def mark(store, slug: str, name: str, now_ms: int, same: bool) -> None:
    key = _key(store, slug, name)
    store.redis.set(key, now_ms, nx=same)
    store.redis.expire(key, DAY_S)


def clear(store, slug: str, name: str) -> None:
    store.redis.delete(_key(store, slug, name))


def since(store, slug: str, name: str) -> int | None:
    raw = store.redis.get(_key(store, slug, name))
    return None if raw is None else int(raw)
