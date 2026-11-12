import atexit
import json
import os
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from functools import lru_cache
from threading import Lock
from typing import TYPE_CHECKING, Any

from . import state
from .bus import StateConflict, StateEntry, StateStore
from .config import BusConfig

if TYPE_CHECKING:
    from .redis_state import RedisStateStore

BACKEND_ENV = "AGENTIHOOKS_SCHEDULER_STATE_BACKEND"
MAX_DOCUMENT = 8 * 1024 * 1024
_lock = Lock()
RECOVERY = Lock()


@lru_cache
def _bus(config: BusConfig):
    from .factory import open_bus

    bus = open_bus(config)
    atexit.register(bus.close)
    return bus


def backend(environ: Mapping[str, str] = os.environ) -> str:
    chosen = environ.get(BACKEND_ENV)
    return state.backend({state.BACKEND_ENV: chosen} if chosen else environ)


def open_scheduler_state(bucket: str) -> StateStore:
    """The scheduler state store; a scoped bus with no swarm setting opens the unscoped state its supervisor reads."""
    if backend() == "bus":
        with _lock:
            config = BusConfig.from_environ()
            swarm = os.environ.get("AGENTIHOOKS_SWARM")
            if config.scoped and swarm:
                return _bus(config).for_swarm(swarm).state(bucket)
            return _bus(replace(config, scoped=False)).state(bucket)
    from scripts.swarm.store import redis_client

    from .redis_state import RedisStateStore

    return RedisStateStore(redis_client(), bucket)


def live(document: dict[str, Any], now: float) -> dict[str, Any]:
    """The scheduler document without retired entries and abandoned process timers."""
    from scripts.scheduler.core import abandoned

    retired = document.get("retired", {})
    entries = document.get("entries", {})
    kept = {key: row for key, row in entries.items() if key not in retired and not abandoned(row, now)}
    return {**document, "entries": kept, "retired": {}}


def shrink(state: "RedisStateStore", key: str) -> None:
    """Rewrites an oversized scheduler document once under a process lock so waiters never each decode it."""
    from .redis_state import WRITE_TRIES

    with RECOVERY:
        for _ in range(WRITE_TRIES):
            if state.redis.hstrlen(state.watch_key, key) <= MAX_DOCUMENT:
                return
            stored = state.get(key)
            if stored is None:
                return
            parsed = json.loads(stored.value)
            document = live(parsed, time.time())
            if document == parsed:
                return
            with suppress(StateConflict):
                state.update(key, json.dumps(document).encode(), stored.revision)


def read(state: StateStore, key: str, legacy_key: str) -> StateEntry | None:
    from scripts.scheduler.core import abandoned

    from .redis_state import WRITE_TRIES, RedisStateStore

    if isinstance(state, RedisStateStore) and state.redis.hstrlen(state.watch_key, key) > MAX_DOCUMENT:
        shrink(state, key)
    for _ in range(WRITE_TRIES):
        stored = state.get(key)
        if not isinstance(state, RedisStateStore):
            return stored
        document = json.loads(stored.value) if stored is not None and stored.value else {}
        if "entries" in document:
            return stored
        now = time.time()
        entries = {}
        for entry_id, raw in state.redis.hscan_iter(legacy_key):
            entry = json.loads(raw)
            if not abandoned(entry, now):
                entries[entry_id] = entry
        retired = [entry_id for entry_id, _ in state.redis.hscan_iter(legacy_key.removesuffix("entries") + "retired")]
        if not entries and not retired:
            return stored
        document.update({"entries": entries, "retired": dict.fromkeys(retired, True)})
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
