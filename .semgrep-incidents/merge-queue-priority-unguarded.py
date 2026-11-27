from collections.abc import Callable
from typing import Any


def operate(queue: Callable[[], Any], priority: Callable[[], Any]) -> dict[str, Any]:
    queue()
    # ruleid: merge-queue-priority-unguarded
    return {"priority": priority()}


def prioritized(priority: Callable[[], Any]) -> dict[str, Any]:
    try:
        # ok: merge-queue-priority-unguarded
        return {"priority": priority()}
    except Exception as exc:
        return {"priority_error": str(exc)}
