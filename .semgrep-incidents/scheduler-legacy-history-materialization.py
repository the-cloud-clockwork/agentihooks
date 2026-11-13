from collections.abc import Iterator
from typing import Any


def unbounded(state: Any, key: str) -> Any:
    # ruleid: scheduler-legacy-history-materialization
    return state.redis.hgetall(key)


def bounded(state: Any, key: str) -> Iterator[tuple[str, str]]:
    # ok: scheduler-legacy-history-materialization
    yield from state.redis.hscan_iter(key, count=128)
