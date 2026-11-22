"""Live check of the saved expert missions: every running lineage of every swarm that is not stopped still holds the
mission text its address names. It prints one line per running lineage and never an account or a credential."""

import sys
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any, Protocol

from scripts.swarm import expert_lineage
from scripts.swarm.store import SwarmError

FAIL = "FAIL: "
SAVED = "{slug} lineage {id} runs at {effort} with its mission saved as {mission}"


class Missions(Protocol):
    """What the check reads of the swarm store: the swarms, each one's state and its keys."""

    redis: Any

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...

    def key(self, slug: str, *parts: str) -> str: ...


def lines(store: Missions) -> list[str]:
    """One line per running lineage of the swarms that are not stopped, a failing one where its saved mission is gone
    or no longer matches its address, or one line that says no lineage runs."""
    found: list[str] = []
    for slug in sorted(store.slugs()):
        if store.config(slug).state == "stopped":
            continue
        for lineage in expert_lineage.running(store, slug):
            try:
                expert_lineage.mission(store, slug, lineage)
            except SwarmError as fault:
                found.append(f"{FAIL}{slug} lineage {lineage.id}: {fault}")
            else:
                found.append(SAVED.format(slug=slug, **asdict(lineage)))
    return found or [expert_lineage.NO_RUNNING]


def main(store: Missions | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    found = lines(store)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
