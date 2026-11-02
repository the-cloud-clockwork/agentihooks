import fcntl
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from scripts.events.bus import StateStore

from hooks.context import operator_mode
from scripts.scheduler import Scheduler


@contextmanager
def state_lock(session_id: str) -> Generator[None]:
    path = operator_mode._path(session_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cancel(session_id: str, state: dict[str, Any]) -> None:
    previous = state.pop("timer_id", None)
    operator_mode._save(session_id, state)
    if previous:
        Scheduler().get(previous).cancel()


def expire(event: dict[str, Any], delivery_id: str, store: "StateStore") -> None:
    session_id = cast(str, event["owner"])
    with state_lock(session_id):
        state = cast(
            dict[str, Any],
            operator_mode._load(session_id),
        )
        if state.get("timer_id") != event["id"]:
            return
        state.pop("timer_id")
        operator_mode._save(session_id, state)
