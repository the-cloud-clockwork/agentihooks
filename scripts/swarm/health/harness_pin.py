"""Lane harness pins: a health finding for each lane pinned to one harness while the other harness has free quota seats
and ready lane tasks wait, read from the pins the capacity pass stores with its quota decision.

The pin age is the time since a capacity pass first saw the pin, because the swarm config keeps no time for it.
"""

from collections.abc import Mapping
from typing import Any

from scripts import quota_freshness
from scripts.swarm import capacity
from scripts.swarm.health.findings import Finding

KIND = "harness pin"
THRESHOLD = "a free seat on the other harness"


def age(pin: Mapping[str, Any], now_ms: int) -> str:
    """The time since a capacity pass first saw ``pin``, as the quota lines write a span."""
    return quota_freshness.age(pin["since"] / 1000, now_ms / 1000)


def _finding(lane: str, pin: Mapping[str, Any], now_ms: int) -> Finding:
    agent, other, waiting = capacity.LABELS[pin["agent"]], capacity.LABELS[pin["other"]], pin["waiting"]
    summary = (
        f"Lane {lane} is pinned to {agent}, first seen {age(pin, now_ms)} ago, while {other} has {pin['free']} free "
        f"quota seats and {len(waiting)} ready tasks wait"
    )
    evidence = (
        f"The {lane} lane agent is {pin['agent']}, so capacity offers its tasks only {agent} seats",
        f"{other} free quota seats: {pin['free']}",
        f"Ready {lane} tasks waiting: {', '.join(waiting)}",
        f"Pin first seen by the capacity pass {age(pin, now_ms)} ago",
    )
    return Finding(KIND, lane, summary, evidence, THRESHOLD, len(waiting))


def findings(store: Any, slug: str, now_ms: int) -> list[Finding]:
    """One finding per lane pinned to one harness while the other harness has free quota seats and ready tasks wait."""
    pinned = sorted(capacity.read(store, slug).get("pins", {}).items())
    return [_finding(lane, pin, now_ms) for lane, pin in pinned if pin["free"] and pin["waiting"]]
