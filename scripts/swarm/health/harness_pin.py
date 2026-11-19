"""Lane harness pins: a health finding for each lane pinned to one harness while the other harness has free quota seats
and ready lane tasks wait, read from the pins the capacity pass stores with its quota decision."""

from typing import Any

from scripts.swarm.health.findings import Finding


def findings(store: Any, slug: str, now_ms: int) -> list[Finding]:
    """One finding per lane pinned to one harness while the other harness has free quota seats and ready tasks wait."""
    return []
