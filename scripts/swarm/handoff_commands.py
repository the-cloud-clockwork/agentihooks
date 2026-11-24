"""The confirm-handoff verb of the swarm command line: only a live session confirms the handoff it took over, the
confirmed transfer is printed, and an expert's confirmation takes the handoff off the seat of its lineage."""

import json
from collections.abc import Collection
from typing import Any

from scripts.handoff import transfers
from scripts.swarm import expert_seat
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError


def confirm(
    store: RedisStore, slug: str, transfer: str, agent: AgentRecord, next_action: str, live: Collection[str], at: int
) -> None:
    """Confirm at ``at`` the handoff of ``transfer`` that ``agent`` took over, by the first Next action it read, and
    print the transfer. Only a session among the ``live`` ones confirms, and an expert's confirmation takes the
    handoff off the seat of its lineage."""
    if agent.name not in live:
        raise SwarmError("Only a live successor can confirm its handoff")
    result: dict[str, Any] = transfers.confirm(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        store, slug, transfer, agent, next_action, at
    )
    expert_seat.drop_confirmed(store, slug, agent)
    print(json.dumps(result))
