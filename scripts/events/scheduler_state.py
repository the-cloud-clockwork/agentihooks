import atexit
import json
import os
from functools import lru_cache
from threading import Lock

from . import state
from .bus import StateConflict, StateEntry, StateStore
from .config import BusConfig

_lock = Lock()


@lru_cache
def _bus(config: BusConfig):
    from .factory import open_bus

    bus = open_bus(config)
    atexit.register(bus.close)
    return bus


def open_scheduler_state(bucket: str) -> StateStore:
    if state.backend() == "bus":
        with _lock:
            config = BusConfig.from_environ()
            bus = _bus(config)
            if config.scoped:
                bus = bus.for_swarm(os.environ["AGENTIHOOKS_SWARM"])
            return bus.state(bucket)
    return state.open_state(bucket)


def read(state: StateStore, key: str, legacy_key: str) -> StateEntry | None:
    from .redis_state import WRITE_TRIES, RedisStateStore

    for _ in range(WRITE_TRIES):
        stored = state.get(key)
        if not isinstance(state, RedisStateStore):
            return stored
        document = json.loads(stored.value) if stored is not None and stored.value else {}
        if "entries" in document:
            return stored
        entries = state.redis.hgetall(legacy_key)
        retired = state.redis.hgetall(legacy_key.removesuffix("entries") + "retired")
        if not entries and not retired:
            return stored
        document.update(
            {
                "entries": {entry_id: json.loads(raw) for entry_id, raw in entries.items()},
                "retired": dict.fromkeys(retired, True),
            }
        )
        try:
            if stored is None:
                state.create(key, json.dumps(document).encode())
            else:
                state.update(key, json.dumps(document).encode(), stored.revision)
        except StateConflict:
            continue
        return state.get(key)
    raise StateConflict(f"scheduler migration {key!r} kept changing")


def wake(state: StateStore, key: str, delivery_id: str) -> None:
    from .redis_state import RedisStateStore

    if isinstance(state, RedisStateStore):
        with state.redis.pipeline() as pipe:
            pipe.rpush(key, delivery_id)
            pipe.ltrim(key, -1, -1)
            pipe.execute()
        return
    try:
        state.create(key.replace(":", "."), delivery_id.encode())
    except StateConflict:
        pass
