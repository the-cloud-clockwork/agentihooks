import base64
import json
from collections.abc import Callable, Iterator
from typing import Any

from .bus import StateConflict, StateEntry

HISTORY = 2
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
    def __init__(
        self,
        redis: Any,
        bucket: str,
        prefix: str | None = None,
        projection: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.redis = redis
        base = f"{prefix or _prefix()}:state:{bucket}"
        self._current = f"{base}:current"
        self._revision = f"{base}:revision"
        self._history = f"{base}:history:"
        self._projected = f"{base}:projected"
        self._published = f"{base}:published"
        self.projection = projection

    @property
    def watch_key(self) -> str:
        return self._current

    @property
    def revision_key(self) -> str:
        return self._revision

    def guard(self, pipe: Any) -> None:
        pipe.watch(self._current)

    def get(self, key: str) -> StateEntry | None:
        return _entry(key, self.redis.hget(self._current, key))

    def get_projection(self, key: str) -> StateEntry | None:
        with self.redis.pipeline() as pipe:
            pipe.get(self._revision)
            pipe.get(self._published)
            pipe.hget(self._projected, key)
            revision, published, raw = pipe.execute()
        return _entry(key, raw) if revision == published else None

    def get_many(self, keys: list[str]) -> list[StateEntry | None]:
        if not keys:
            return []
        return [_entry(key, raw) for key, raw in zip(keys, self.redis.hmget(self._current, keys))]

    def create(self, key: str, value: bytes) -> int:
        return self._write(key, value, None, f"state key {key!r} exists")

    def update(self, key: str, value: bytes, revision: int) -> int:
        return self._write(key, value, revision, f"state key {key!r} is not at revision {revision}")

    def _write(self, key: str, value: bytes, revision: int | None, conflict: str) -> int:
        from redis.exceptions import WatchError

        for _ in range(WRITE_TRIES):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self._current, self._revision)
                    found = pipe.hget(self._current, key)
                    if (None if found is None else json.loads(found)["revision"]) != revision:
                        raise StateConflict(conflict)
                    published = pipe.get(self._published)
                    current = pipe.get(self._revision)
                    consistent = current == published
                    written = int(current or 0) + 1
                    row = json.dumps({"revision": written, "value": base64.b64encode(value).decode()})
                    pipe.multi()
                    pipe.incr(self._revision)
                    if self.projection is not None or published is not None:
                        self._project(pipe, key, value, written, consistent)
                    pipe.hset(self._current, key, row)
                    pipe.rpush(self._history + key, row)
                    pipe.ltrim(self._history + key, -HISTORY, -1)
                    pipe.execute()
                    return written
                except WatchError:
                    continue
        raise StateConflict(f"state key {key!r} kept changing")

    def _project(self, pipe: Any, key: str, value: bytes, revision: int, consistent: bool) -> None:
        if not consistent:
            pipe.delete(self._projected)
        if self.projection is None:
            pipe.hdel(self._projected, key)
        else:
            row = json.dumps({"revision": revision, "value": base64.b64encode(self.projection(value)).decode()})
            pipe.hset(self._projected, key, row)
        pipe.set(self._published, revision)

    def watch(self, key: str, from_revision: int = 0) -> Iterator[StateEntry]:
        for raw in self.redis.lrange(self._history + key, 0, -1):
            entry = _entry(key, raw)
            if entry.revision >= from_revision:
                yield entry
