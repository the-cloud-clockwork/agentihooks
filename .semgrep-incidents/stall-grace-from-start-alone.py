from typing import Any


def floored(agent: Any, pushed_at: int) -> int:
    # ruleid: stall-grace-from-start-alone
    return max(pushed_at, agent.started_at if agent else 0)


def launched(agent: Any, pushed_at: int) -> int:
    # ok: stall-grace-from-start-alone
    return max(pushed_at, max(agent.started_at, agent.launched_at) if agent else 0)
