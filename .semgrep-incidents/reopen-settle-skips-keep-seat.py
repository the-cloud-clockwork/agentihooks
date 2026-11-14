from typing import Any

from scripts.inbox import exits
from scripts.swarm import tick


def drop_without_seat(slug: str, store: Any, ledger: Any, rows: dict[str, Any], agent: Any, expected: Any) -> None:
    store.release(slug, agent.task, agent.name)
    # ruleid: reopen-settle-skips-keep-seat
    reopen = tick._reopen(slug, ledger, rows, agent.task, expected)
    _settle_exit(store, agent.name, agent.seat if reopen else "")


def watchdog_without_seat(watch: Any, tid: str, owner: str, agent: Any) -> None:
    # ruleid: reopen-settle-skips-keep-seat
    reopened = watch.ledger.update_task(watch.slug, tid, {"state": "open", "claimed_by": ""}, if_state=HELD)
    if reopened.get("state") == "open" and agent is not None:
        watch.store.drop_agent(watch.slug, owner)
        exits.settle(InboxStore(watch.store.redis), owner, agent.seat, STOPPED)


def drop_keeping_seat(slug: str, store: Any, ledger: Any, rows: dict[str, Any], agent: Any, expected: Any) -> None:
    # ok: reopen-settle-skips-keep-seat
    reopen = tick._reopen(slug, ledger, rows, agent.task, expected)
    if reopen and agent.seat:
        store.keep_seat(slug, agent.task, agent.seat)
    _settle_exit(store, agent.name, agent.seat if reopen else "")


def watchdog_keeping_seat(watch: Any, tid: str, owner: str, agent: Any) -> None:
    # ok: reopen-settle-skips-keep-seat
    reopened = watch.ledger.update_task(watch.slug, tid, {"state": "open", "claimed_by": ""}, if_state=HELD)
    if reopened.get("state") == "open" and agent is not None:
        if watch.store.drop_agent(watch.slug, owner) is not False and agent.seat:
            watch.store.keep_seat(watch.slug, tid, agent.seat)
        exits.settle(InboxStore(watch.store.redis), owner, agent.seat, STOPPED)


def reopen_without_exit(slug: str, ledger: Any, rows: dict[str, Any], task_id: str) -> bool:
    # ok: reopen-settle-skips-keep-seat
    return tick._reopen(slug, ledger, rows, task_id)
