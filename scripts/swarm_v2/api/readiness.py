"""Open readiness checks Redis using a separate connection with bounded waits and no retries."""

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
