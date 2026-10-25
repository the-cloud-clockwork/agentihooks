import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING

from scripts.scheduler import service
from scripts.scheduler.core import Scheduler

if TYPE_CHECKING:
    from redis import Redis

RECHECK_S = 30
# BLPOP treats a zero timeout as block forever.
MIN_POP_S = 0.01


def blocking_client() -> "Redis":
    from redis import ConnectionPool, Redis

    from scripts.swarm.store import redis_client

    source = redis_client()
    pool = source.connection_pool
    options = {**pool.connection_kwargs, "socket_timeout": RECHECK_S + 5}
    client = Redis(connection_pool=ConnectionPool(connection_class=pool.connection_class, **options))
    source.close()
    return client


def wake(event: dict, delivery_id: str, redis: "Redis") -> None:
    key = json.loads(event["payload"])["key"]
    with redis.pipeline() as pipe:
        pipe.rpush(key, delivery_id)
        pipe.ltrim(key, -1, -1)
        pipe.execute()


def _recheck(deadline: float | None) -> float:
    if deadline is None:
        return RECHECK_S
    return min(RECHECK_S, max(MIN_POP_S, deadline - time.monotonic()))


class Timer:
    def __init__(self, delay: float, *, redis: "Redis | None" = None) -> None:
        self.closed = False
        self.owns_redis = redis is None
        self.redis = redis if redis is not None else blocking_client()
        scheduler = Scheduler(self.redis)
        self.key = scheduler.key(f"wake:{uuid.uuid4().hex}")
        self.entry = scheduler.timer(owner="process", payload=json.dumps({"key": self.key}), delay=delay)
        self.entry.timeout.connect(wake)
        self.entry.start()
        try:
            service.ensure_running()
        except BaseException:
            self.close()
            raise

    def wait(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.redis.blpop(self.key, timeout=_recheck(deadline)) is None:
            if deadline is not None and time.monotonic() >= deadline:
                return
            service.ensure_running()

    def close(self) -> None:
        if self.closed:
            return
        self.entry.scheduler.retire(self.entry.id, self.key)
        self.closed = True
        self.redis.delete(self.key)
        if self.owns_redis:
            self.redis.close()

    def __enter__(self) -> "Timer":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def wait(delay: float, redis: "Redis | None" = None, timeout: float | None = None) -> None:
    with Timer(delay, redis=redis) as timer:
        timer.wait(timeout)


async def async_wait(delay: float) -> None:
    with Timer(delay) as timer:
        waiting = asyncio.create_task(asyncio.to_thread(timer.wait))
        try:
            await asyncio.shield(waiting)
        finally:
            if not waiting.done():
                timer.redis.rpush(timer.key, "cancelled")
                await waiting
