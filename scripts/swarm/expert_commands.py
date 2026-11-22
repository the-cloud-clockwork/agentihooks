"""The expert verb of the swarm command line: the mission template, starting and stopping a lineage, where each
lineage stands and the mission an expert reads.

Starting and stopping belong to the master of the swarm and the operator. An expert reads the mission of its own
lineage and no other.
"""

from typing import Any

from scripts.handoff.transfers import now_ms

__all__ = ["now_ms", "register"]


def register(sub: Any) -> None:
    """Add the expert verb and its actions to the swarm command parser."""
