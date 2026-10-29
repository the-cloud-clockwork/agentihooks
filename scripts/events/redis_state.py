import base64
import json
from collections.abc import Iterator

from .bus import StateConflict, StateEntry

HISTORY = 64
WRITE_TRIES = 16


def _prefix() -> str:
    from scripts.swarm.store import PREFIX

    return PREFIX


def _entry(key: str, raw: str | None) -> StateEntry | None:
    if raw is None:
        return None
    row = json.loads(raw)
    return StateEntry(key=key, value=base64.b64decode(row["value"]), revision=row["revision"])


class RedisStateStore:
    def __init__(self, redis, bucket: str, prefix: str | None = None):
        self.redis = redis
        base = f"{prefix or _prefix()}:state:{bucket}"
        self._current = f"{base}:current"
        self._revision = f"{base}:revision"
        self._history = f"{base}:history:"

    def get(self, key: str) -> StateEntry | None:
        return _entry(key, self.redis.hget(self._current, key))

    def create(self, key: str, value: bytes) -> int:
        return self._write(key, value, None, f"state key {key!r} exists")

    def update(self, key: str, value: bytes, revision: int) -> int:
        return self._write(key, value, revision, f"state key {key!r} is not at revision {revision}")

    def _write(self, key: str, value: bytes, revision: int | None, conflict: str) -> int:
        from redis.exceptions import WatchError

        for _ in range(WRITE_TRIES):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self._current)
                    found = pipe.hget(self._current, key)
                    if (None if found is None else json.loads(found)["revision"]) != revision:
                        raise StateConflict(conflict)
                    written = self.redis.incr(self._revision)
                    row = json.dumps({"revision": written, "value": base64.b64encode(value).decode()})
                    pipe.multi()
                    pipe.hset(self._current, key, row)
                    pipe.rpush(self._history + key, row)
                    pipe.ltrim(self._history + key, -HISTORY, -1)
                    pipe.execute()
                    return written
                except WatchError:
                    continue
        raise StateConflict(f"state key {key!r} kept changing")

    def watch(self, key: str, from_revision: int = 0) -> Iterator[StateEntry]:
        for raw in self.redis.lrange(self._history + key, 0, -1):
            entry = _entry(key, raw)
            if entry.revision >= from_revision:
                yield entry
