"""The swarm key value stores' backend: AGENTIHOOKS_STATE_BACKEND picks Redis (the default) or the configured event bus."""

import os
from collections.abc import Mapping

from .bus import StateConflict, StateStore

BACKEND_ENV = "AGENTIHOOKS_STATE_BACKEND"
BACKENDS = ("bus", "redis")


def backend(environ: Mapping[str, str] = os.environ) -> str:
    chosen = environ.get(BACKEND_ENV) or "redis"
    if chosen not in BACKENDS:
        raise ValueError(f"unknown state backend {chosen!r}; known: {', '.join(BACKENDS)}")
    return chosen


def open_state(bucket: str, environ: Mapping[str, str] = os.environ) -> StateStore:
    if backend(environ) == "bus":
        from .config import BusConfig
        from .factory import open_bus

        return open_bus(BusConfig.from_environ(environ)).state(bucket)
    from scripts.swarm.store import redis_client

    from .redis_state import RedisStateStore

    return RedisStateStore(redis_client(environ), bucket)


def unavailable(environ: Mapping[str, str] = os.environ) -> tuple[type[BaseException], ...]:
    if backend(environ) == "bus":
        from nats.errors import Error

        return (Error, OSError, RuntimeError, StateConflict)
    from redis.exceptions import RedisError

    return (RedisError, OSError, StateConflict)
