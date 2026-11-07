"""One Redis client per hook process, short timeouts and one ping, shared by the heartbeat, inbox and gates."""

import importlib
import os
from collections.abc import MutableMapping
from typing import Any

CONNECT_TIMEOUT_S = 0.5
SOCKET_TIMEOUT_S = 1.0

_clients: dict[tuple[str, int], Any] = {}


def client(environ: MutableMapping[str, str] | None = None) -> Any:
    """Return this process's client for the swarm Redis; a ping that failed once raises again without a new try."""
    importlib.import_module("hooks.config")
    store: Any = importlib.import_module("scripts.swarm.store")
    env = os.environ if environ is None else environ
    store._adopt_saved_member(env)
    key = (str(store.redis_url(env)), os.getpid())
    held = _clients.get(key)
    if held is None:
        held = _clients[key] = _connect(key[0], env)
    if isinstance(held, BaseException):
        raise held
    return held


def _connect(url: str, environ: MutableMapping[str, str]) -> Any:
    """Build and ping a short timeout client; a refused password takes the swarm store's hive recovery path."""
    import redis

    factory: Any = redis.Redis
    found = factory.from_url(
        url, decode_responses=True, socket_connect_timeout=CONNECT_TIMEOUT_S, socket_timeout=SOCKET_TIMEOUT_S
    )
    try:
        found.ping()
        return found
    except redis.AuthenticationError:
        return _recovered(environ)
    except redis.RedisError as exc:
        return exc


def _recovered(environ: MutableMapping[str, str]) -> Any:
    """Return the swarm store's client after its hive password refresh, or the error it raised."""
    import redis

    store: Any = importlib.import_module("scripts.swarm.store")
    try:
        return store.redis_client(environ)
    except redis.RedisError as exc:
        return exc
