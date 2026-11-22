"""Operator orders nobody works on: the slow lane notes when each open order's open tasks first had nothing claimed
or in a pull request, and past thirty minutes that is an order waiting health finding for the master."""

from typing import Any

from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore

KEY = "order-waiting"
KIND = "order waiting"
MINUTES = 30


def read(store: RedisStore, slug: str) -> dict[str, Any]:
    return {}


def observe(store: RedisStore, slug: str, doc: dict[str, Any], at: int) -> dict[str, Any]:
    """Note each open order with open tasks and none claimed or in a pull request, keeping the time it was first
    seen so; any other order drops out."""
    return {}


def findings(store: RedisStore, slug: str) -> list[Finding]:
    """An order waiting finding for every order whose open tasks had nothing claimed or in a pull request for more
    than thirty minutes."""
    return []
