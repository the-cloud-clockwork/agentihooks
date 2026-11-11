"""Seats left waiting on a restore decision past one tick window, a health finding for the master."""

from scripts.swarm import lease, resume
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import RedisStore

KIND = "restore wait"


def findings(store: RedisStore, slug: str, now_ms: int) -> list[Finding]:
    waiting = {
        agent.name: agent
        for agent in store.agents(slug)  # pyright: ignore[reportUnknownMemberType]
        if agent.state == resume.AWAITING
    }
    found: list[Finding] = []
    for row in store.restored(slug):  # pyright: ignore[reportUnknownMemberType]
        agent = waiting.get(row["name"])
        if agent is None:
            continue
        since = row.get("awaited_at") or row["at"]
        if now_ms - since <= lease.tick_ms():
            continue
        minutes = (now_ms - since) // MINUTE_MS
        found.append(
            Finding(
                KIND,
                agent.name,
                f"seat {agent.seat or agent.name} has waited {minutes} minutes on a resume or fresh restore decision",
                (f"restore outcome: {row['reason']}", f"task {agent.task or 'none'}"),
                "over one tick window",
                minutes,
            )
        )
    return found
