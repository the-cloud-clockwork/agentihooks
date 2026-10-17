import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager

from hooks.context import operator_mode
from scripts.scheduler import Scheduler
from scripts.scheduler.slots import InboxSlot

EXPIRED_NOTICE = "Operator off: no typed prompt arrived before the operator timer expired."


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


def start(session_id: str, state: dict, now: float) -> None:
    from scripts.scheduler.service import ensure_running
    from scripts.swarm.store import redis_client

    scheduler = Scheduler(redis_client())
    timer = scheduler.timer(
        owner=session_id,
        payload=EXPIRED_NOTICE,
        delay=float(os.environ.get("AGENTIHOOKS_OPERATOR_TIMEOUT_SEC", "300")),
        now=now,
    )
    timer.timeout.connect(expire)
    state["timer_id"] = timer.id
    operator_mode._save(session_id, state)
    timer.start(now=now)
    ensure_running()


def expire(event: dict, delivery_id: str, redis) -> None:
    session_id = event["owner"]
    with state_lock(session_id):
        state = operator_mode._load(session_id)
        if state.get("timer_id") != event["id"]:
            return
        state.update(on=False, turn_at=None)
        operator_mode._save(session_id, state)
        InboxSlot(session_id).deliver(event, delivery_id, redis)
