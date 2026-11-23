from typing import Any

from redis.exceptions import NoPermissionError

from scripts.swarm import queue_record
from scripts.swarm.queue_priority import apply
from scripts.swarm.store import RedisStore, SwarmError


def automatic_bad(store: RedisStore, slug: str, url: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    # ruleid: hive-queue-priority-permission
    try:
        return apply(store, slug, url, "swarm", tasks, queue_record.Journal(url))
    except SwarmError as exc:
        return {"url": url, "errors": [str(exc)]}


def automatic_good(store: RedisStore, slug: str, url: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    # ok: hive-queue-priority-permission
    try:
        return apply(store, slug, url, "swarm", tasks, queue_record.Journal(url))
    except (SwarmError, NoPermissionError) as exc:
        return {"url": url, "errors": [str(exc)]}
