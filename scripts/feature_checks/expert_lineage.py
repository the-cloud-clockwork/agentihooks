"""Live check of the expert launch pin: every working expert of every swarm that is not stopped runs Claude Code on
the Fable model at effort xhigh or max. It prints one line per expert and never an account or a credential."""

import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from scripts.swarm import expert_launch

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord

PIN = f"{expert_launch.HARNESS} {expert_launch.MODEL} at {' or '.join(expert_launch.EFFORTS)}"
RUNS = "{slug} expert {name} runs {harness} {model} at {effort}"
OFF = f"FAIL: {{line}}, off the pin {PIN}"
NONE = "no working expert"


class Fleet(Protocol):
    """What the check reads of the swarm store: the swarms, each one's state and its agent records."""

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...

    def agents(self, slug: str) -> Iterable["AgentRecord"]: ...


def experts(store: Fleet) -> list[tuple[str, "AgentRecord"]]:
    """Each working expert with its swarm, over the swarms that are not stopped, in swarm then name order."""
    return [
        (slug, agent)
        for slug in sorted(store.slugs())
        if store.config(slug).state != "stopped"
        for agent in sorted(store.agents(slug), key=lambda found: found.name)
        if agent.lane == expert_launch.LANE and agent.state == "working"
    ]


def pinned(agent: "AgentRecord") -> bool:
    """Whether the launch recorded for the agent is the expert pin."""
    return (
        agent.harness == expert_launch.HARNESS
        and expert_launch.MODEL in agent.model.lower()
        and agent.effort in expert_launch.EFFORTS
    )


def main(store: Fleet | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    found = experts(store)
    failed = 0
    for slug, agent in found:
        line = RUNS.format(slug=slug, name=agent.name, harness=agent.harness, model=agent.model, effort=agent.effort)
        if pinned(agent):
            print(line)
        else:
            failed = 1
            print(OFF.format(line=line))
    if not found:
        print(NONE)
    return failed


if __name__ == "__main__":
    sys.exit(main())
