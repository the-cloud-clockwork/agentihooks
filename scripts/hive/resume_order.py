"""Runs one hub resume order for a hive in its own process, off the hive heartbeat."""

import json
import sys
from typing import Any, cast


def run(hive: str, raw: str) -> None:
    """Answer the resume order ``raw`` on ``hive`` and store its acknowledgement."""
    from scripts.hive.orders import CommandOrder, Consumer
    from scripts.swarm import store

    redis: Any = cast(Any, store).redis_client()
    try:
        Consumer.local(redis, hive).run_resume(CommandOrder(**json.loads(raw)))
    finally:
        redis.close()


if __name__ == "__main__":
    run(*sys.argv[1:])
