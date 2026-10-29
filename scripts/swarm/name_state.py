"""The names state store behind the swarm name registry, with reads of the Redis keys written before it."""

import hashlib
import json

from scripts.events import state
from scripts.events.bus import StateConflict

BUCKET = "names"
WRITE_TRIES = 16
WHOLE = ("number", "spawned_at", "retired_at")
MISSING = object()
KEEP = object()
_OPENED = {}


class NamingError(RuntimeError):
    pass


def state_key(kind, part):
    return f"{kind}.{hashlib.sha256(part.encode()).hexdigest()}"


def shared_store(client):
    if state.backend() == "redis":
        from scripts.events.redis_state import RedisStateStore

        return RedisStateStore(client, BUCKET)
    if BUCKET not in _OPENED:
        _OPENED[BUCKET] = state.open_state(BUCKET)
    return _OPENED[BUCKET]


def value(entry):
    if entry is None:
        return MISSING
    return json.loads(entry.value) if entry.value else None


def read_many(store, keys):
    many = getattr(store, "get_many", None)
    return many(keys) if many is not None else [store.get(key) for key in keys]


def row(raw):
    return {k: int(v) if k in WHOLE else v for k, v in raw.items()} if raw else None


class NameState:
    """Each legacy reader takes the Redis client and answers the value written before the store; it runs only on
    the Redis backend and only for a key the store does not hold yet."""

    def __init__(self, redis, shared=None):
        self.shared = shared if shared is not None else shared_store(redis)
        self.legacy = redis if state.backend() == "redis" else None

    def watched(self, legacy_key):
        """The Redis keys a transaction watches to see a registry key change, its legacy key and the store."""
        shared = getattr(self.shared, "watch_key", None)
        return [legacy_key, *([shared] if shared and self.legacy is not None else [])]

    def views(self, reader):
        if reader is None or self.legacy is None:
            return self.shared, self.legacy
        from scripts.events.redis_state import RedisStateStore

        return RedisStateStore(reader, BUCKET), reader

    def get(self, kind, part, legacy, reader=None):
        shared, client = self.views(reader)
        found = value(shared.get(state_key(kind, part)))
        if found is MISSING:
            return legacy(client) if client is not None else None
        return found

    def get_many(self, kind, parts, legacy):
        """legacy takes the client and the parts the store lacks, and answers their values in that order."""
        values = [value(entry) for entry in read_many(self.shared, [state_key(kind, part) for part in parts])]
        missing = list(dict.fromkeys(part for part, found in zip(parts, values) if found is MISSING))
        found = dict(zip(missing, legacy(self.legacy, missing))) if missing and self.legacy is not None else {}
        return [found.get(part) if current is MISSING else current for part, current in zip(parts, values)]

    def amend(self, kind, part, change, legacy):
        return self.write(state_key(kind, part), change, legacy)

    def write(self, key, change, legacy):
        """Writes change(current) at the revision it read, retrying on a race; KEEP writes nothing, None removes."""
        for _ in range(WRITE_TRIES):
            entry = self.shared.get(key)
            current = value(entry)
            if current is MISSING:
                current = legacy(self.legacy) if self.legacy is not None else None
            changed = change(current)
            if changed is KEEP:
                return current
            if changed is None and current is None:
                return None
            encoded = b"" if changed is None else json.dumps(changed).encode()
            try:
                if entry is None:
                    self.shared.create(key, encoded)
                else:
                    self.shared.update(key, encoded, entry.revision)
                return changed
            except StateConflict:
                continue
        raise NamingError(f"name registry key {key} kept changing")

    def restore(self, rows):
        for key, found in rows.items():
            self.write(key, lambda _, found=found: found, lambda _: None)
