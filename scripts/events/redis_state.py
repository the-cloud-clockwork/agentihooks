import base64
import json
from collections.abc import Callable, Iterator
from typing import Any

from .bus import StateConflict, StateEntry

HISTORY = 64
WRITE_TRIES = 16
_WRITE = """
local found = redis.call('HGET', KEYS[1], ARGV[1])
local stored = ''
if found then
  stored = string.match(found, '"revision":%s*(%d+)') or '?'
end
if stored ~= ARGV[2] then
  return -1
end
local published = redis.call('GET', KEYS[3])
local consistent = redis.call('GET', KEYS[2]) == published
local written = redis.call('INCR', KEYS[2])
local row = '{"revision": ' .. written .. ', "value": "' .. ARGV[3] .. '"}'
if ARGV[4] == '1' or published then
  if not consistent then
    redis.call('DEL', KEYS[4])
  end
  if ARGV[4] == '1' then
    redis.call('HSET', KEYS[4], ARGV[1], '{"revision": ' .. written .. ', "value": "' .. ARGV[5] .. '"}')
  else
    redis.call('HDEL', KEYS[4], ARGV[1])
  end
  redis.call('SET', KEYS[3], written)
end
redis.call('HSET', KEYS[1], ARGV[1], row)
redis.call('RPUSH', KEYS[5], row)
redis.call('LTRIM', KEYS[5], -tonumber(ARGV[6]), -1)
return written
"""


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
        """Compare the key's own revision and write it in one script, so writes to other keys never conflict."""
        written = int(
            self.redis.eval(
                _WRITE,
                5,
                self._current,
                self._revision,
                self._published,
                self._projected,
                self._history + key,
                key,
                "" if revision is None else str(revision),
                base64.b64encode(value).decode(),
                "0" if self.projection is None else "1",
                "" if self.projection is None else base64.b64encode(self.projection(value)).decode(),
                HISTORY,
            )
        )
        if written < 0:
            raise StateConflict(conflict)
        return written

    def watch(self, key: str, from_revision: int = 0) -> Iterator[StateEntry]:
        for raw in self.redis.lrange(self._history + key, 0, -1):
            entry = _entry(key, raw)
            if entry.revision >= from_revision:
                yield entry
