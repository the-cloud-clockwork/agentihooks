"""Runs one hub resume order for a hive in its own process, off the hive heartbeat."""

import json
import sys
from typing import Any

from scripts.hive.orders import FAILED, Ack, CommandOrder, Consumer, Orders, answer


def run(hive: str, raw: str) -> None:
    """Answer the resume order ``raw`` on ``hive``; any failure before or during the resume is stored as its answer."""
    from scripts.swarm import store

    order = CommandOrder(**json.loads(raw))
    redis: Any = store.redis_client()  # pyright: ignore[reportUnknownMemberType]
    try:
        Consumer.local(redis, hive).run_resume(order)
    except Exception as exc:
        answer(redis, Orders(redis).command_receipt(hive, order.id), Ack(FAILED, {}, f"error: {type(exc).__name__}"))
    finally:
        redis.close()


if __name__ == "__main__":
    run(*sys.argv[1:])
