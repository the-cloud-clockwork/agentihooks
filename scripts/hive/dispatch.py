import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.hive.orders import Consumer

LIMIT = 32
_workers: dict[tuple[str, str], subprocess.Popen] = {}


def poll(consumer: "Consumer") -> None:
    for key, process in list(_workers.items()):
        if process.poll() is not None:
            del _workers[key]
    active = sum(hive == consumer.hive for hive, _ in _workers)
    for item, _ in consumer.store.redis.xrange(consumer.orders.stream(consumer.hive), count=LIMIT):
        key = consumer.hive, item
        if key in _workers:
            continue
        if active >= LIMIT:
            return
        _workers[key] = subprocess.Popen(
            [sys.executable, "-m", "scripts.hive.dispatch", consumer.hive, item, consumer.pod],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        active += 1


def run(hive: str, item: str, pod: str) -> None:
    from scripts.hive.daemon import poll_orders
    from scripts.hive.orders import Consumer
    from scripts.swarm.store import redis_client

    redis = redis_client()
    try:
        consumer = Consumer.local(redis, hive)
        consumer.pod = pod
        entries = redis.xrange(consumer.orders.stream(hive), min=item, max=item)
        for entry, fields in entries:
            poll_orders(consumer, lambda: consumer.consume(entry, fields))
    finally:
        redis.close()


if __name__ == "__main__":
    run(*sys.argv[1:])
