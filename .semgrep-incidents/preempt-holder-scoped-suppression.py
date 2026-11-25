import json
from typing import Any


def holder_scoped_pending(asked: dict[str, str], task_id: str, now_ms: int) -> bool:
    # ruleid: preempt-holder-scoped-suppression
    return any(target == task_id and now_ms - at < 600_000 for target, at in map(json.loads, asked.values()))


def task_scoped_pending(redis: Any, key: str, now_ms: int) -> bool:
    at = redis.get(key)
    # ok: preempt-holder-scoped-suppression
    return at is not None and now_ms - int(at) < 600_000
