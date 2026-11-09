import base64
import io
import json
import threading
import time
from importlib import import_module
from typing import Any, BinaryIO

from redis.exceptions import WatchError

from . import redis_stream
from .bus import StateConflict, StateEntry
from .redis_state import WRITE_TRIES, RedisStateStore

MAX_DOCUMENT = 8 * 1024 * 1024
_recovery = threading.RLock()


def compact(source: BinaryIO | io.RawIOBase, now: float) -> dict[str, Any]:
    from scripts.scheduler.core import abandoned as is_abandoned

    document: dict[str, Any] = {"entries": {}, "retired": {}}
    builder: Any = None
    target = ""
    parser: Any = import_module("ijson")
    builder_type: Any = import_module("ijson.common").ObjectBuilder
    for prefix, event, value in parser.parse(source, use_float=True):
        if builder is not None:
            builder.event(event, value)
            if prefix == target and event in {"end_map", "end_array"}:
                record = builder.value
                if target.startswith("entries."):
                    if not is_abandoned(record, now):
                        document["entries"][target.removeprefix("entries.")] = record
                else:
                    document[target] = record
                builder = None
            continue
        if prefix == "retired" and event == "map_key":
            document["entries"].pop(value, None)
        elif prefix.startswith("entries.") and event == "start_map":
            builder, target = builder_type(), prefix
            builder.event(event, value)
        elif prefix and "." not in prefix and prefix not in {"entries", "retired"}:
            if event in {"start_map", "start_array"}:
                builder, target = builder_type(), prefix
                builder.event(event, value)
            elif event not in {"map_key", "end_map", "end_array"}:
                document[prefix] = value
    return document


class SchedulerState(RedisStateStore):
    def __init__(self, redis: Any, bucket: str) -> None:
        super().__init__(redis, bucket, projection=lambda value: value)

    def _records(self, key: str) -> str:
        return f"{self._projected}:{key}"

    def _project(self, pipe: Any, key: str, value: bytes, revision: int, consistent: bool) -> None:
        if not key.endswith(".entries"):
            return
        entries = json.loads(value).get("entries", {})
        pipe.delete(self._records(key))
        rows = {name: json.dumps(row) for name, row in entries.items()}
        rows["_revision"] = str(revision)
        pipe.hset(self._records(key), mapping=rows)

    def scheduler_record(self, key: str, entry_id: str) -> dict[str, Any] | None:
        raw = self.redis.hget(self._records(key), entry_id)
        if raw is not None:
            return json.loads(raw)
        if self.redis.hexists(self._records(key), "_revision"):
            return None
        stored = self.get(key)
        if stored is None:
            return None
        return json.loads(stored.value).get("entries", {}).get(entry_id)

    def get(self, key: str) -> StateEntry | None:
        if key.endswith(".entries") and self.redis.hstrlen(self._current, key) > MAX_DOCUMENT:
            with _recovery:
                if self.redis.hstrlen(self._current, key) > MAX_DOCUMENT:
                    self._compact(key)
        return super().get(key)

    def _compact(self, key: str) -> None:
        for _ in range(WRITE_TRIES):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self._current, self._revision)
                    with redis_stream.field(self.redis, self._current, key) as raw:
                        if raw is None:
                            return
                        document = compact(redis_stream.Envelope(raw), time.time())
                    revision = int(pipe.get(self._revision) or 0) + 1
                    value = json.dumps(document, sort_keys=True).encode()
                    row = json.dumps({"revision": revision, "value": base64.b64encode(value).decode()})
                    pipe.multi()
                    pipe.incr(self._revision)
                    pipe.hset(self._current, key, row)
                    pipe.delete(self._history + key)
                    pipe.rpush(self._history + key, row)
                    self._project(pipe, key, value, revision, False)
                    pipe.execute()
                    self._prune_legacy(key.replace(".", ":"))
                    return
                except WatchError:
                    continue
        raise StateConflict(f"scheduler recovery {key!r} kept changing")

    def _prune_legacy(self, key: str) -> None:
        from scripts.scheduler.core import abandoned as is_abandoned

        now, cursor = time.time(), 0
        while True:
            cursor, rows = self.redis.hscan(key, cursor=cursor, count=128)
            abandoned = [name for name, raw in rows.items() if is_abandoned(json.loads(raw), now)]
            if abandoned:
                self.redis.hdel(key, *abandoned)
            if not cursor:
                return
