def read(repo: str) -> tuple:
    return ()


def _decoded(saved: object) -> object:
    return saved


def _saved_or_read(store: object, slug: str, repo: str) -> tuple:
    saved = store.redis.get(store.key(slug, f"fleet-red-pulls:{repo}"))
    # ruleid: claim-view-live-network-read-on-cache-miss
    if saved is None:
        return read(repo)
    return _decoded(saved) or ()


def _saved_only(store: object, slug: str, repo: str) -> tuple:
    saved = store.redis.get(store.key(slug, f"fleet-red-pulls:{repo}"))
    # ok: claim-view-live-network-read-on-cache-miss
    if saved is None:
        return ()
    return _decoded(saved) or ()


def _cached_or_listed_elsewhere(store: object, slug: str, repo: str, cache: object) -> object:
    saved = cache.get(repo)
    # ok: claim-view-live-network-read-on-cache-miss
    if saved is None:
        return _decoded(store.redis.get(store.key(slug, repo))) or ()
    return saved
