"""Live check that an expert claims nothing: in every swarm that is not stopped, no ledger task is claimed by an
expert. It prints one line per swarm and never an account or a credential."""

import sys
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from scripts.swarm import expert_launch, naming
from scripts.swarm.store import SwarmError

FAIL = "FAIL: "
CLEAN = "{slug} holds {count} tasks and none is claimed by an expert"
CLAIMED = "{slug} task {task} is claimed by the expert {name}"
UNREAD = "{slug}: its tasks could not be read: {fault}"
NONE = "no swarm is running"

Tasks = Callable[[str], list[dict[str, Any]]]


class Fleet(Protocol):
    """What the check reads of the swarm store: the swarms and each one's state."""

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...


def judged(slug: str, tasks: Tasks) -> list[str]:
    """The lines for one swarm: one failing line per task an expert claims, or one line that says none is."""
    try:
        rows = tasks(slug)
    except SwarmError as fault:
        return [FAIL + UNREAD.format(slug=slug, fault=fault)]
    claimed = [
        FAIL + CLAIMED.format(slug=slug, task=row["id"], name=row["claimed_by"])
        for row in rows
        if naming.lane_of(row.get("claimed_by") or "") == expert_launch.LANE
    ]
    return claimed or [CLEAN.format(slug=slug, count=len(rows))]


def lines(store: Fleet, tasks: Tasks) -> list[str]:
    """The lines of every swarm that is not stopped, in swarm order, or one line that says none runs."""
    found = [
        line for slug in sorted(store.slugs()) if store.config(slug).state != "stopped" for line in judged(slug, tasks)
    ]
    return found or [NONE]


def main(store: Fleet | None = None, tasks: Tasks | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    if tasks is None:
        from scripts.swarm.ledger_client import LedgerClient

        tasks = LedgerClient().tasks
    found = lines(store, tasks)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
