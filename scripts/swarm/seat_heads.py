"""Which listed agents still head their seat, so a handed off predecessor kept listed for its reap is passed over."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord


def superseded(agent: AgentRecord, listed: Iterable[AgentRecord]) -> bool:
    """Whether ``agent`` finished and a newer execution on its seat is listed beside it."""
    return agent.state == "finished" and any(
        other.seat == agent.seat and other.generation > agent.generation for other in listed
    )
