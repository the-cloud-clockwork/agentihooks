"""The state a health check keeps between tick passes: one JSON value under the check's own key of the swarm.

The value is private to the check's step, so the step declares no write for it. A value another step reads is a
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


def write(store: RedisStore, slug: str, key: str, at: int, **rows: Any) -> None:
    """Keep the check's rows with the time of this pass, the time first."""
    client: Any = store
    client.redis.set(store.key(slug, key), json.dumps({"at": at, **rows}))
