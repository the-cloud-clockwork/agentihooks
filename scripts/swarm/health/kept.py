"""The state a health check keeps between tick passes: one JSON value under the check's own key of the swarm.

The value belongs to the check's module alone: its observe half writes it in the check's own step and its findings
half reads it in the health findings step, so no step declares a write for it. A value another module reads is a
declared domain, written through a port, and does not belong here.
"""

import json
from typing import Any

from scripts.swarm.store import RedisStore


def read(store: RedisStore, slug: str, key: str) -> dict[str, Any]:
    """The value the check kept at its last pass, empty when it kept none."""
    client: Any = store
    raw: str | None = client.redis.get(store.key(slug, key))
    return json.loads(raw) if raw else {}


def write(store: RedisStore, slug: str, key: str, at: int, name: str, rows: Any) -> None:
    """Keep the check's rows under their name with the time of this pass, the time first."""
    if name == "at":
        raise ValueError("a check keeps its rows under a name other than at, the time of its pass")
    client: Any = store
    client.redis.set(store.key(slug, key), json.dumps({"at": at, name: rows}))
