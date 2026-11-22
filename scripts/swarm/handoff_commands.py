"""The handoff verbs of the swarm command line, once the command line knows who calls: what confirming a handoff
does."""

import argparse
import json
from collections.abc import Collection
from typing import Any

from scripts.handoff import transfers
from scripts.handoff.transfers import now_ms
from scripts.swarm import expert_seat
from scripts.swarm.store import AgentRecord, SwarmError


def confirm(store: Any, args: argparse.Namespace, agent: AgentRecord, live: Collection[str]) -> None:
    """Confirm the handoff ``agent`` took over and print its transfer. Only a session among the ``live`` ones
    confirms, and an expert's confirmation takes the handoff off the seat of its lineage."""
    if agent.name not in live:
        raise SwarmError("Only a live successor can confirm its handoff")
    result: dict[str, Any] = transfers.confirm(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        store, args.slug, args.transfer, agent, args.next, now_ms()
    )
    expert_seat.drop_confirmed(store, args.slug, agent)
    print(json.dumps(result))
