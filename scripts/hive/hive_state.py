"""Hive records, the hive index and hive credentials through the swarm state protocol.

On Redis each hive's record gets its own store, so a member's ACL user is granted its own record and no other.
"""

import json
from collections.abc import Callable

from scripts.events import state
from scripts.events.bus import StateConflict, StateStore
from scripts.events.redis_state import RedisStateStore
from scripts.swarm.keyspace import ROOT

RECORDS = "hives"
CREDENTIALS = "hive-credentials"
# A hive id starts with a letter or digit, so this key never names a hive.
INDEX = "_index"
LEGACY_INDEX = f"{ROOT}:hives"
TRIES = 16
KEEP = object()
_OPENED: dict[str, StateStore] = {}


def on_redis() -> bool:
    return state.backend() == "redis"


def _bus(bucket: str) -> StateStore:
    if bucket not in _OPENED:
        _OPENED[bucket] = state.open_state(bucket)
    return _OPENED[bucket]


def records(client, hive_id: str) -> StateStore:
    return RedisStateStore(client, f"hive-{hive_id}") if on_redis() else _bus(RECORDS)


def credentials(client, prefix: str) -> StateStore:
    return RedisStateStore(client, "credentials", prefix) if on_redis() else _bus(CREDENTIALS)


def read(store: StateStore, key: str, legacy: Callable[[], object] = lambda: None):
    entry = store.get(key)
    if entry is None:
        return legacy()
    return json.loads(entry.value) if entry.value else None


def amend(store: StateStore, key: str, change: Callable, legacy: Callable[[], object] = lambda: None):
    """Writes change(current) at the revision it read, retrying on a race; KEEP writes nothing, None removes."""
    for _ in range(TRIES):
        entry = store.get(key)
        if entry is None:
            current = legacy()
        else:
            current = json.loads(entry.value) if entry.value else None
        changed = change(current)
        if changed is KEEP:
            return current
        if changed is None and current is None:
            return None
        value = b"" if changed is None else json.dumps(changed).encode()
        try:
            if entry is None:
                store.create(key, value)
            else:
                store.update(key, value, entry.revision)
            return changed
        except StateConflict:
            continue
    raise StateConflict(f"hive state key {key!r} kept changing")


def _legacy_index(client) -> Callable[[], list[str]]:
    return lambda: sorted(client.smembers(LEGACY_INDEX)) if on_redis() else []


def index(client) -> list[str]:
    return read(records(client, INDEX), INDEX, _legacy_index(client)) or []


def indexed(client, hive_id: str, present: bool) -> None:
    def change(current):
        ids = set(current or ())
        wanted = ids | {hive_id} if present else ids - {hive_id}
        return KEEP if wanted == ids and current is not None else sorted(wanted)

    amend(records(client, INDEX), INDEX, change, _legacy_index(client))
    # Redis readers that are not on the protocol yet, the launch slot script among them, still read the legacy set.
    if on_redis():
        (client.sadd if present else client.srem)(LEGACY_INDEX, hive_id)
