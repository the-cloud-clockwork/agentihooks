"""Which contract a handoff document is held to: the expert handoff for an expert, Handoff v2 for everyone else."""

from collections.abc import Callable
from typing import Any

from scripts.handoff import check, expert
from scripts.swarm import expert_launch, expert_lineage
from scripts.swarm.store import AgentRecord


def problems(text: str, resolves: Callable[[str], object], store: Any, slug: str, agent: AgentRecord) -> list[str]:
    """The problems of a handoff document under the contract its author is held to: an expert's against the saved
    mission of its lineage, any other agent's against Handoff v2."""
    if agent.lane != expert_launch.LANE:
        return check.problems(text, resolves)
    lineage = expert_lineage.get(store, slug, agent.task)
    return expert.problems(text, resolves, slug, expert_lineage.mission(store, slug, lineage))
