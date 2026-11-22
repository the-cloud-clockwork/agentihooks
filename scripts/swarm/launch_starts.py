"""agentihooks swarm <slug> launches: each current agent's acknowledged hive and launch time, read through the public
events reader."""

import argparse
import json
from typing import Any

from scripts.events import factory
from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hive import execution_stop
from scripts.swarm.store import AgentRecord

STARTED = "execution.started"


def _own(event: Envelope, slug: str, agent: AgentRecord) -> bool:
    """True when the acknowledgement is the started event of the agent's own execution."""
    data: dict[str, Any] = event.data
    found = (event.type, event.swarm, event.key, event.version, data.get("name"), data.get("task"))
    return found == (STARTED, slug, agent.execution_id, agent.generation, agent.name, agent.task)


def _row(bus: Bus, slug: str, agent: AgentRecord) -> dict[str, Any]:
    """The agent's report row, with null hive and launch time when its own execution has no acknowledgement."""
    event = execution_stop.started(bus, slug, agent.name, agent.generation)
    if event is None or not _own(event, slug, agent):
        return {"agent": agent.name, "hive": None, "launched_at": None}
    data: dict[str, Any] = event.data
    return {"agent": agent.name, "hive": data.get("hive"), "launched_at": data.get("launched_at", event.time)}


def command(store: Any, args: argparse.Namespace) -> None:
    """Print one row per current agent ordered by name; a failed read prints nothing and still closes the bus."""
    agents: list[AgentRecord] = store.agents(args.slug)
    bus = factory.open_bus(BusConfig.from_environ())
    try:
        rows = [_row(bus, args.slug, agent) for agent in sorted(agents, key=lambda agent: agent.name)]
    finally:
        bus.close()
    print(json.dumps(rows))
