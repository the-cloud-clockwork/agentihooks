"""Open readiness checks Redis using a separate connection with bounded waits and no retries."""

import threading
from typing import Any


def available(client: Any) -> bool:
    from redis import ConnectionPool, Redis, RedisError
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    source = client.connection_pool
    options = source.connection_kwargs | {
        "socket_timeout": 2,
        "socket_connect_timeout": 2,
        "retry": Retry(NoBackoff(), 0),
        "retry_on_timeout": False,
        "retry_on_error": [],
        "health_check_interval": 0,
    }
    pool = ConnectionPool(connection_class=source.connection_class, **options)
    try:
        return bool(Redis(connection_pool=pool).ping())
    except RedisError:
        return False
    finally:
        pool.disconnect()


class Probe:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.running = threading.Lock()

    def check(self) -> bool:
        if not self.running.acquire(blocking=False):
            return False
        done = threading.Event()
        outcome: list[bool] = []

        def ping() -> None:
            try:
                outcome.append(available(self.client))
            finally:
                done.set()
                self.running.release()

        threading.Thread(target=ping, daemon=True).start()
        return done.wait(4) and outcome[0]
