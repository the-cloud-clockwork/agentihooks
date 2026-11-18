from collections.abc import Callable
from typing import Any

from scripts.swarm.store import store_errors


def stand_by_until_redis_refuses(lock: Any, wait: Callable[[float], None]) -> None:
    if not lock.take():
        # ruleid: controller-standby-unguarded-redis-call
        lock.lead(wait)


def release_until_redis_refuses(lock: Any) -> None:
    # ruleid: controller-standby-unguarded-redis-call
    lock.release()


def stand_by_through_a_redis_error(lock: Any, wait: Callable[[float], None]) -> bool:
    try:
        if not lock.take():
            # ok: controller-standby-unguarded-redis-call
            lock.lead(wait)
    except store_errors() as exc:
        print(exc)
        wait(5.0)
        return False
    return True


def release_through_a_redis_error(lock: Any) -> None:
    try:
        # ok: controller-standby-unguarded-redis-call
        lock.release()
    except store_errors() as exc:
        print(exc)
