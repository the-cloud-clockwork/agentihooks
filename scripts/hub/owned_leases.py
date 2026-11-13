"""Claim lease renewal for the agents the hub owns, which the tick reap pass leaves out of its judging."""

from collections.abc import Sequence
from typing import Any

from scripts.events.config import BusConfig
from scripts.swarm.store import AgentRecord


def renew(store: Any, slug: str, legacy: Sequence[AgentRecord], lease_ms: int) -> None:
    """Renew the claim lease of every agent of the swarm outside ``legacy``.

    Its hive published its execution start, so the reap pass leaves it to the hub and only this renewal keeps its claim
    past the first lease. A claim naming another agent, or one already lapsed, is left alone; the memory bus backend
    owns no agent.
    """
    if BusConfig.from_environ().backend == "memory":
        return
    judged = {agent.name for agent in legacy}
    for agent in store.agents(slug):
        if agent.name not in judged:
            store.refresh(slug, agent.task, agent.name, lease_ms)
