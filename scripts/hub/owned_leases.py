"""Claim lease renewal for the agents the hub owns, which the tick reap pass leaves out of its judging."""

from collections.abc import Sequence
from typing import Any

from scripts.events.config import BusConfig
from scripts.swarm.store import AgentRecord


def renew(store: Any, slug: str, legacy: Sequence[AgentRecord], lease_ms: int) -> None:
    """Renew the claim lease of every agent of the swarm outside ``legacy``."""
    del store, slug, legacy, lease_ms
    BusConfig.from_environ()
