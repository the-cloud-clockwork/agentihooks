from typing import Any


def scanned(store: Any, slug: str, part: str) -> Any:
    # ruleid: swarm-part-scan-ungated
    return store.redis.scan_iter(match=store.key(slug, part, "*"))


def paged(store: Any, slug: str, part: str) -> Any:
    # ruleid: swarm-part-scan-ungated
    return store.redis.scan(0, match=store.key(slug, part, "*"), count=1000)


def listed(store: Any, slug: str, part: str) -> Any:
    # ruleid: swarm-part-scan-ungated
    return store.redis.keys(store.key(slug, part, "*"))


def gated(store: Any, slug: str, part: str) -> Any:
    if not store.redis.exists(store.key(slug, "control-epoch")):
        return iter(())
    # ok: swarm-part-scan-ungated
    return store.redis.scan_iter(match=store.key(slug, part, "*"))


def other_guard(store: Any, slug: str, part: str, marker: Any) -> Any:
    if not marker.exists():
        return iter(())
    # ruleid: swarm-part-scan-ungated
    return store.redis.scan_iter(match=store.key(slug, part, "*"))


def whole_swarm(store: Any, slug: str) -> Any:
    # ok: swarm-part-scan-ungated
    return store.redis.scan_iter(match=store.key(slug, "*"))
