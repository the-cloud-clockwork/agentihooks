"""Live check of the expert lineage over every swarm that is not stopped: each working expert runs Claude Code on the
Fable model at effort xhigh or max, and each lineage's saved mission still matches its address. It prints one line
per expert and per lineage and never an account or a credential."""

import sys
from collections.abc import Iterable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Protocol

from scripts.swarm import expert_launch, expert_lineage
from scripts.swarm.store import SwarmError

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord

FAIL = "FAIL: "
PIN = f"{expert_launch.HARNESS} {expert_launch.MODEL} at {' or '.join(expert_launch.EFFORTS)}"
RUNS = "{slug} expert {name} runs {harness} {model} at {effort}"
OFF = f"{FAIL}{{line}}, off the pin {PIN}"
NONE = "no working expert"
SAVED = "{slug} lineage {id} is {state} at {effort} with its mission saved as {mission}"


class Fleet(Protocol):
    """What the check reads of the swarm store: the swarms, each one's state, its agent records and its keys."""

    redis: Any

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...

    def agents(self, slug: str) -> Iterable["AgentRecord"]: ...

    def key(self, slug: str, *parts: str) -> str: ...


def _live(store: Fleet) -> list[str]:
    return [slug for slug in sorted(store.slugs()) if store.config(slug).state != "stopped"]


def experts(store: Fleet) -> list[tuple[str, "AgentRecord"]]:
    """Each working expert with its swarm, over the swarms that are not stopped, in swarm then name order."""
    return [
        (slug, agent)
        for slug in _live(store)
        for agent in sorted(store.agents(slug), key=lambda found: found.name)
        if agent.lane == expert_launch.LANE and agent.state == "working"
    ]


def pinned(agent: "AgentRecord") -> bool:
    """Whether the launch recorded for the agent is the expert pin."""
    return expert_launch.on_pin(agent.harness, agent.model, agent.effort)


def launches(store: Fleet) -> list[str]:
    """One line per working expert, a failing one for each that is off the pin, or one line that says none works."""
    lines: list[str] = []
    for slug, agent in experts(store):
        line = RUNS.format(slug=slug, name=agent.name, harness=agent.harness, model=agent.model, effort=agent.effort)
        lines.append(line if pinned(agent) else OFF.format(line=line))
    return lines or [NONE]


def missions(store: Fleet) -> list[str]:
    """One line per lineage of the swarms that are not stopped, a failing one where its saved mission is gone or
    no longer matches its address."""
    lines: list[str] = []
    for slug in _live(store):
        for lineage in expert_lineage.listed(store, slug):
            try:
                expert_lineage.mission(store, slug, lineage)
            except SwarmError as fault:
                lines.append(f"{FAIL}{slug} lineage {lineage.id}: {fault}")
            else:
                lines.append(SAVED.format(slug=slug, **asdict(lineage)))
    return lines


def main(store: Fleet | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    lines = [*launches(store), *missions(store)]
    for line in lines:
        print(line)
    return int(any(line.startswith(FAIL) for line in lines))


if __name__ == "__main__":
    sys.exit(main())
