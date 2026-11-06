"""Keep a master another hive runs live in a tick that cannot see that hive's panes or processes."""

from dataclasses import replace
from typing import Any

from scripts.hive import reports
from scripts.swarm import idle
from scripts.swarm.store import MASTER, AgentRecord


def holders(store: Any) -> dict[str, set[str]]:
    """Each agent name every live hive report lists, with the hives that list it."""
    live: dict[str, dict[str, Any]] = reports.Reports(store.redis).live()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    found: dict[str, set[str]] = {}
    for hive, report in live.items():
        names: list[str] = report.get("names", [])
        for name in names:
            found.setdefault(name, set()).add(hive)
    return found


def keep(slug: str, store: Any, agent: AgentRecord, listed: dict[str, set[str]], now_ms: int) -> bool:
    """Keep a master live unjudged, back from retiring, while another hive lists it and its own heartbeat is fresh."""
    from scripts.swarm.commands import hive_id

    target: dict[str, Any] = agent.runtime_target  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if agent.lane != MASTER or target.get("host_id") or not listed.get(agent.name, set()) - {hive_id()}:
        return False
    beat: dict[str, Any] | None = idle.heartbeat(store.redis, slug, agent.name)  # pyright: ignore[reportUnknownMemberType]
    if beat is None or now_ms - beat["at"] > reports.LOST_MS:
        return False
    if agent.state == "retiring":
        store.put_agent(slug, replace(agent, state="working"))
    return True
