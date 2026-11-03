"""Tick passes that keep ending early on the same missing pass value: one streak per swarm, cleared by a full pass."""

import json
import os
from collections.abc import Mapping
from typing import Any

from scripts.swarm.health.findings import Finding

KEY = "early-end-streak"
KIND = "early end"
ENV = "AGENTIHOOKS_HEALTH_EARLY_ENDS"
DEFAULT = 3


def read(store: Any, slug: str) -> dict[str, Any]:
    """Return the swarm's streak, the missing ``value`` and its ``count``, or an empty dict after a full pass."""
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def note(store: Any, slug: str, value: str) -> None:
    """Count one more pass ending early on ``value``, starting again at one when the last missed another value."""
    last = read(store, slug)
    count = last["count"] + 1 if last.get("value") == value else 1
    store.redis.set(store.key(slug, KEY), json.dumps({"value": value, "count": count}))


def clear(store: Any, slug: str) -> None:
    """End the streak, as a full pass does."""
    store.redis.delete(store.key(slug, KEY))


def threshold(environ: Mapping[str, str]) -> int:
    """Return the streak length that raises the finding, from the health setting when it is a positive whole number."""
    raw = environ.get(ENV, "")
    return int(raw) if raw.isdecimal() and int(raw) > 0 else DEFAULT


def findings(store: Any, slug: str, environ: Mapping[str, str] = os.environ) -> list[Finding]:
    """Return one information only finding once the streak reaches the threshold, naming the value and the count."""
    streak, limit = read(store, slug), threshold(environ)
    if streak.get("count", 0) < limit:
        return []
    value, count = streak["value"], streak["count"]
    return [
        Finding(
            KIND,
            slug,
            f"Tick passes keep ending early because no step stored {value}",
            (f"{count} passes in a row ended early on {value} while the heartbeat was stamped",),
            f"{limit} passes in a row",
            count,
        )
    ]
