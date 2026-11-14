from typing import Any

from scripts.swarm import tick


def free_without_stop(
    slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any
) -> list[str]:
    if not tick.retire_watch.retire_unless_gone(store, runtime, agent, (), now_ms):
        return ["retrying"]
    tick.resume.free_seat(store, slug, agent, now_ms)
    return [
        "freed"
        # ruleid: tick-free-skips-execution-stop
        + tick._drop(slug, store, ledger, rows, agent, refund=True)
    ]


def free_dropping_before_a_stop_of_something_else(
    slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any
) -> list[str]:
    if not tick.retire_watch.retire_unless_gone(store, runtime, agent, (), now_ms):
        return ["retrying"]
    # ruleid: tick-free-skips-execution-stop
    dropped = tick._drop(slug, store, ledger, rows, agent, refund=True)
    tick._stop_hive(slug, agent, "stuck", "")
    return ["freed" + dropped]


def free_through_the_stop(
    slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any
) -> list[str]:
    if not tick.retire_watch.retire_unless_gone(store, runtime, agent, (), now_ms):
        return ["retrying"]
    # ok: tick-free-skips-execution-stop
    dropped = tick._stop_hive(slug, agent, "refused", tick._drop(slug, store, ledger, rows, agent, refund=True))
    return ["freed" + dropped]


def free_then_stop(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if not tick.retire_watch.retire_unless_gone(store, runtime, agent, (), now_ms):
        return ["retrying"]
    # ok: tick-free-skips-execution-stop
    dropped = tick.timing.timed(tick._drop, slug, store, ledger, rows, agent, refund=True)
    tick._stop_hive(slug, agent, "idle", dropped)
    return ["freed" + dropped]


def drop_without_retire(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any) -> str:
    # ok: tick-free-skips-execution-stop
    return tick._drop(slug, store, ledger, rows, agent, refund=True)
