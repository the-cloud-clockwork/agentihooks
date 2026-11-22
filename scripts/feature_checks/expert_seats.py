"""Live check of the expert seats: in every swarm that is not stopped, the seat of a running lineage is held by at
most one expert, and every expert that holds a seat belongs to a lineage the swarm started. It prints one line per
running lineage and never an account or a credential."""

import sys
from typing import Any, Protocol

from scripts.feature_checks.expert_missions import Missions
from scripts.swarm import expert_launch, expert_lineage
from scripts.swarm.store import SwarmError

FAIL = "FAIL: "
WORKED = "{slug} lineage {id} is worked by {name}"
VACANT = "{slug} lineage {id} has no expert on its seat"
CROWDED = "{slug} lineage {id} is held by {count} experts: {names}"
STRAY = "{slug} expert {name} holds the seat of {task}, which is no lineage of the swarm"
NONE = "no running expert lineage"


class Seats(Missions, Protocol):
    """What the check reads of the swarm store: what the missions check reads, and the agents of a swarm."""

    def agents(self, slug: str) -> list[Any]: ...


def lines(store: Seats) -> list[str]:
    """One line per running lineage of the swarms that are not stopped, a failing one for a seat two experts hold
    and for an expert of a lineage the swarm never started, or one line that says no lineage runs."""
    found: list[str] = []
    for slug in sorted(store.slugs()):
        if store.config(slug).state == "stopped":
            continue
        held: dict[str, list[str]] = {}
        for agent in store.agents(slug):
            if agent.lane == expert_launch.LANE and agent.state != "finished":
                held.setdefault(agent.task, []).append(agent.name)
        for lineage in expert_lineage.running(store, slug):
            found.append(_seat(slug, lineage.id, sorted(held.pop(lineage.id, []))))
        found += [_stray(store, slug, task, name) for task in sorted(held) for name in sorted(held[task])]
    return [line for line in found if line] or [NONE]


def _seat(slug: str, lineage: str, names: list[str]) -> str:
    if len(names) > 1:
        return FAIL + CROWDED.format(slug=slug, id=lineage, count=len(names), names=", ".join(names))
    if names:
        return WORKED.format(slug=slug, id=lineage, name=names[0])
    return VACANT.format(slug=slug, id=lineage)


def _stray(store: Seats, slug: str, task: str, name: str) -> str:
    """The failing line for an expert whose seat is no lineage of the swarm; empty for the expert of a lineage that
    ended, which the next pass of the tick finishes."""
    try:
        expert_lineage.get(store, slug, task)
    except SwarmError:
        return FAIL + STRAY.format(slug=slug, name=name, task=task)
    return ""


def main(store: Seats | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    found = lines(store)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
