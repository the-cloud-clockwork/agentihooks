import asyncio
import json
import uuid
from typing import TYPE_CHECKING

from scripts.scheduler import service
from scripts.scheduler.core import Scheduler

if TYPE_CHECKING:
    from redis import Redis


def blocking_client() -> "Redis":
    from redis import ConnectionPool, Redis

    from scripts.swarm.store import redis_client

    source = redis_client()
    pool = source.connection_pool
    options = {**pool.connection_kwargs, "socket_timeout": None}
    client = Redis(connection_pool=ConnectionPool(connection_class=pool.connection_class, **options))
    source.close()
    return client


def wake(event: dict, delivery_id: str, redis: "Redis") -> None:
    key = json.loads(event["payload"])["key"]
    with redis.pipeline() as pipe:
        pipe.rpush(key, delivery_id)
        pipe.ltrim(key, -1, -1)
        pipe.execute()


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

    def wait(self) -> None:
        self.redis.blpop(self.key)

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


def wait(delay: float) -> None:
    with Timer(delay) as timer:
        timer.wait()


async def async_wait(delay: float) -> None:
    with Timer(delay) as timer:
        waiting = asyncio.create_task(asyncio.to_thread(timer.wait))
        try:
            await asyncio.shield(waiting)
        finally:
            if not waiting.done():
                timer.redis.rpush(timer.key, "cancelled")
                await waiting
