"""Swarm sessions whose context passed the compact limit plus the handoff margin with neither a prepare nor a recycle
marker: the recycle directive never reached them."""

from collections.abc import Callable

from scripts.swarm.health.findings import Finding

KIND = "context overrun"


def findings(agents: list[dict], overrun: Callable[[str], int | None]) -> list[Finding]:
    found = []
    for agent in agents:
        session = agent["conversation_id"]
        used = overrun(session) if session else None
        if used is None:
            continue
        found.append(
            Finding(
                KIND,
                agent["name"],
                "swarm session passed the hard gate with no handoff preparation or recycle",
                (f"session {session}", f"context {used // 1000}k tokens", f"state {agent['state']}"),
                "context past the session's compact limit plus margin with no prepare or recycle marker",
                used // 1000,
            )
        )
    return found
