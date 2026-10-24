import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

from hooks.context import operator_mode
from scripts.scheduler import Scheduler


@contextmanager
def state_lock(session_id: str) -> Iterator[None]:
    path = operator_mode._path(session_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cancel(session_id: str, state: dict) -> None:
    from scripts.swarm.store import redis_client

    previous = state.pop("timer_id", None)
    operator_mode._save(session_id, state)
    if previous:
        Scheduler(redis_client()).get(previous).cancel()


def expire(event: dict, delivery_id: str, redis: "Redis") -> None:
    session_id = event["owner"]
    with state_lock(session_id):
        state = operator_mode._load(session_id)
        if state.get("timer_id") != event["id"]:
            return
        state.pop("timer_id")
        operator_mode._save(session_id, state)
