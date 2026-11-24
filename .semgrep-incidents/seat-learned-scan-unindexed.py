from typing import Any

PREFIX = "agentihooks:seat"


def every_call(redis: Any, slug: str) -> list[Any]:
    pattern = f"{PREFIX}:*@{slug}:learned"
    # ruleid: seat-learned-scan-unindexed
    return list(redis.scan_iter(match=pattern, count=1000))


def inline(redis: Any, slug: str) -> list[Any]:
    # ruleid: seat-learned-scan-unindexed
    return list(redis.scan_iter(match=f"{PREFIX}:*@{slug}:learned"))


def once(redis: Any, slug: str) -> set[Any]:
    index = f"{PREFIX}-learned:{slug}"
    if not redis.exists(f"{index}:listed"):
        pattern = f"{PREFIX}:*@{slug}:learned"
        # ok: seat-learned-scan-unindexed
        found = list(redis.scan_iter(match=pattern, count=1000))
        redis.sadd(index, *found)
        redis.set(f"{index}:listed", 1)
    return redis.smembers(index)


def claimed(redis: Any, slug: str) -> set[Any]:
    index = f"{PREFIX}-learned:{slug}"
    if redis.set(f"{index}:listed", 1, nx=True, ex=3600):
        pattern = f"{PREFIX}:*@{slug}:learned"
        # ok: seat-learned-scan-unindexed
        found = list(redis.scan_iter(match=pattern, count=1000))
        redis.sadd(index, *found)
    return redis.smembers(index)


def claimed_after_listed(redis: Any, slug: str) -> set[Any]:
    index = f"{PREFIX}-learned:{slug}"
    if not redis.exists(f"{index}:listed") and redis.set(f"{index}:claim", 1, nx=True, ex=60):
        pattern = f"{PREFIX}:*@{slug}:learned"
        # ok: seat-learned-scan-unindexed
        found = list(redis.scan_iter(match=pattern, count=1000))
        redis.sadd(index, *found)
        redis.set(f"{index}:listed", 1, ex=3600)
    return redis.smembers(index)


def unclaimed(redis: Any, slug: str) -> list[Any]:
    if redis.set(f"{PREFIX}-learned:{slug}:listed", 1):
        # ruleid: seat-learned-scan-unindexed
        return list(redis.scan_iter(match=f"{PREFIX}:*@{slug}:learned"))
    return []


def recaps(redis: Any, slug: str) -> list[Any]:
    # ok: seat-learned-scan-unindexed
    return list(redis.scan_iter(match=f"{PREFIX}:*@{slug}:recaps"))
