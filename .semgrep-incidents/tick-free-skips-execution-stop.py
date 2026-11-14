from typing import Any

from scripts.hub.reconciler import stop_admitted
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


def free_with_timed_stop(
    slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any
) -> list[str]:
    if not tick.retire_watch.retire_unless_gone(store, runtime, agent, (), now_ms):
        return ["retrying"]
    tick.timing.timed(stop_admitted, slug, agent, "stuck")
    return [
        "freed"
        # ok: tick-free-skips-execution-stop
        + tick._drop(slug, store, ledger, rows, agent, refund=True)
    ]


def free_with_stop(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, agent: Any, now_ms: Any) -> list[str]:
    if not tick.retire_watch.retire_unless_gone(store, runtime, agent, (), now_ms):
        return ["retrying"]
    stop_admitted(slug, agent, "revoked")
    # ok: tick-free-skips-execution-stop
    return ["freed" + tick._drop(slug, store, ledger, rows, agent, refund=True)]


def drop_without_retire(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any) -> str:
    # ok: tick-free-skips-execution-stop
    return tick._drop(slug, store, ledger, rows, agent, refund=True)
