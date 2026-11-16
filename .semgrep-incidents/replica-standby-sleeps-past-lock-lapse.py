from collections.abc import Callable
from typing import Any


def stand_by_a_whole_poll(lock: Any, wait: Callable[[float], None], poll_s: float) -> None:
    # ruleid: replica-standby-sleeps-past-lock-lapse
    while not lock.take():
        wait(poll_s)


def stand_by_until_the_lapse(lock: Any, wait: Callable[[float], None], poll_s: float) -> None:
    # ok: replica-standby-sleeps-past-lock-lapse
    while not lock.take():
        left_ms = lock.redis.pttl(lock.key)
        if left_ms != -2:
            wait(poll_s if left_ms < 0 else min(poll_s, left_ms / 1000))
