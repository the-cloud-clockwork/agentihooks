"""A tick step's Redis budget: each call waits at most CALL_TIMEOUT_S, and once the step's seconds are spent every
further call refuses, so a stalled Redis holds the step for its budget and one call, never longer."""

import copy
import time

CALL_TIMEOUT_S = 5


class Spent(Exception):
    pass


class Budget:
    def __init__(self, client, seconds: float):
        self._client, self._seconds = client, seconds
        self._deadline = time.monotonic() + seconds
        self.why = ""

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if time.monotonic() >= self._deadline:
                self.why = f"its {self._seconds} second Redis budget is spent"
                raise Spent(self.why)
            return attr(*args, **kwargs)

        return call


def capped(client):
    from redis import ConnectionPool, Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    if type(client) is not Redis:
        return client
    pool = client.connection_pool
    options = pool.connection_kwargs | {"socket_timeout": CALL_TIMEOUT_S, "retry": Retry(NoBackoff(), 0)}
    return Redis(connection_pool=ConnectionPool(connection_class=pool.connection_class, **options))


def release(client, source) -> None:
    if client is not source:
        client.connection_pool.disconnect()


def bounded(store, budget: Budget):
    source, bound = store.redis, copy.copy(store)
    for name, value in vars(store).items():
        if value is source:
            setattr(bound, name, budget)
        elif getattr(value, "redis", None) is source:
            part = copy.copy(value)
            part.redis = budget
            setattr(bound, name, part)
    return bound
