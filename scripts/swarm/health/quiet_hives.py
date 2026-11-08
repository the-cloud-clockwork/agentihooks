"""One health finding per lasting hive that went quiet; the tick sweep removes quiet ephemeral hives instead."""

from typing import Any

from scripts.hive import registry
from scripts.swarm.health.findings import MINUTE_MS, Finding

KIND = "quiet hive"
THRESHOLD = f"no heartbeat for {registry.QUIET_MS // MINUTE_MS} minutes"


def findings(redis: Any, now_ms: int) -> list[Finding]:
    """Return a finding for each non ephemeral hive whose last heartbeat is older than the registry quiet window."""
    found: list[Finding] = []
    for record in registry.hives(redis):
        if record["ephemeral"] == "yes" or not registry.quiet(record, now_ms):
            continue
        minutes = (now_ms - record["heartbeat_at"]) // MINUTE_MS
        found.append(
            Finding(
                KIND,
                record["id"],
                f"Hive {record['name']} has sent no heartbeat for {minutes} minutes",
                (f"last heartbeat at {record['heartbeat_at']}",),
                THRESHOLD,
                minutes,
            )
        )
    return found
