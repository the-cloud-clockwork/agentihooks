import asyncio
import json
import uuid

from scripts.scheduler import service
from scripts.scheduler.core import Scheduler
from scripts.swarm.store import redis_client


def wake(event: dict, delivery_id: str, redis) -> None:
    key = json.loads(event["payload"])["key"]
    with redis.pipeline() as pipe:
        pipe.rpush(key, delivery_id)
        pipe.ltrim(key, -1, -1)
        pipe.expire(key, 60)
        pipe.execute()


class Timer:
    def __init__(self, delay: float, *, redis=None) -> None:
        self.redis = redis if redis is not None else redis_client()
        scheduler = Scheduler(self.redis)
        self.key = scheduler.key(f"wake:{uuid.uuid4().hex}")
        self.entry = scheduler.timer(owner="process", payload=json.dumps({"key": self.key}), delay=delay)
        self.entry.timeout.connect(wake)
        self.entry.start()
        service.ensure_running()

    def wait(self) -> None:
        self.redis.blpop(self.key)
        self.redis.delete(self.key)

    def close(self) -> None:
        self.entry.cancel()
        self.redis.delete(self.key)

    def __enter__(self):
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
