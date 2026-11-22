"""The Stop gate for an expert: a session of a running lineage does not end while its mission is open.

An expert works one mission to its end. Its turn ends only when the lineage ended, on a mission proof or by its
starter's order, when it handed its seat off, or while it holds a declared wait.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from scripts.gates.base import Call, Decision, Who
from scripts.swarm import expert_launch
from scripts.swarm.naming import lane_of

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

REFUSAL = (
    "Lineage {lineage} still runs, so this session does not end here: an expert works its mission to the end. "
    "Keep working. Near the context limit hand off with `agentihooks swarm {slug} handoff <doc>`. When every "
    "deliverable is proven, end the lineage with `agentihooks swarm {slug} expert complete <proof>`. While you wait "
    'on something, declare it with `agentihooks swarm {slug} wait <minutes> --reason "<what>"`.'
)


class ExpertStop:
    name = "expert-stop"
    default_mode = "enforce"

    def __init__(self, connect: Callable[[], "RedisStore"] | None = None, now: Callable[[], int] | None = None) -> None:
        self._connect, self._now = connect, now

    def matches(self, call: Call) -> bool:
        return not call.tool

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        if not who.pinned or lane_of(who.name) != expert_launch.LANE:
            return Decision()
        from scripts.swarm import expert_lineage, idle
        from scripts.swarm.store import SwarmError

        store = self.connect()
        agents = store.agents(who.swarm)  # pyright: ignore[reportUnknownMemberType]
        agent = next((found for found in agents if found.name == who.name), None)
        if agent is None or agent.state == "finished":
            return Decision()
        try:
            lineage = expert_lineage.get(store, who.swarm, agent.task)
        except SwarmError:
            return Decision()
        held: dict[str, Any] | None = idle.wait(  # pyright: ignore[reportUnknownMemberType]
            store.redis,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            who.swarm,
            who.name,
        )
        if lineage.state != expert_lineage.RUNNING or (held is not None and held["until"] > self.now()):
            return Decision()
        refused: Decision = Decision.deny(  # pyright: ignore[reportUnknownMemberType]
            REFUSAL.format(lineage=lineage.id, slug=who.swarm)
        )
        return refused

    def connect(self) -> "RedisStore":
        if self._connect:
            return self._connect()
        from scripts.swarm.store import connect  # pyright: ignore[reportUnknownVariableType]

        return connect()

    def now(self) -> int:
        if self._now:
            return self._now()
        from scripts.handoff.transfers import now_ms

        return now_ms()
