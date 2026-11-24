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


def recaps(redis: Any, slug: str) -> list[Any]:
    # ok: seat-learned-scan-unindexed
    return list(redis.scan_iter(match=f"{PREFIX}:*@{slug}:recaps"))
