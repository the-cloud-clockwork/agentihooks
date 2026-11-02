from typing import Any

from scripts.inbox.seats import is_seat
from scripts.inbox.store import InboxStore
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmError


def empty_seat(inbox: InboxStore, sender: str, address: str) -> dict[str, Any]:
    address = inbox.names.resolve(address)
    if not is_seat(address):
        return {}
    slug = address.split("@", 1)[1]
    agents: dict[str, Any] = {
        agent.name: agent for agent in RedisStore(inbox.redis).agents(slug) if _live(inbox, agent)
    }
    if inbox.seats.occupant(address).occupant in agents:
        return {}
    result: dict[str, Any] = {
        "warning": f"seat {address} is empty; mail waits for its next occupant",
        "overlapping_agents": [],
    }
    agent = agents.get(inbox.names.resolve(sender))
    if agent:
        try:
            result["overlapping_agents"] = _owners(agents, agent.task, LedgerClient().tasks(slug))
        except (OSError, ValueError, SwarmError):
            result["overlap_error"] = "live task owners unavailable"
    return result


def _live(inbox: InboxStore, agent: Any) -> bool:
    return (
        agent.state != "finished"
        and bool(agent.seat)
        and inbox.seats.occupant(agent.seat).occupant == agent.name
        and not inbox.seats.left(agent.name, agent.seat)
    )


def _owners(agents: dict[str, Any], task: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from scripts.swarm.tick import _sharing  # pyright: ignore[reportPrivateUsage]

    rows: dict[str, Any] = {row["id"]: row for row in tasks}
    mine = rows.get(task)
    if mine is None:
        return []
    return [
        {"name": agent.name, "seat": agent.seat, "task": agent.task, "harness": agent.harness}
        for overlap in _sharing(mine, rows, agents.values())
        if (agent := agents.get(overlap["claimant"])) and agent.task == overlap["task"]
    ]
