"""Redis implementation of the fenced records port: per swarm hash tagged hashes written by one Lua script."""

import json
from collections.abc import Callable, Sequence
from typing import Any

from scripts.tick.ports.records import FencedBatch, LeaseFence, Record, RevisionConflict, StaleLease

__all__ = ["CONFLICT", "LAYOUT", "LOG_CAP", "META", "RedisRecords", "STALE"]

META = "meta"
LAYOUT = "layout"
LOG_CAP = 1000
STALE = "FENCE_STALE"
CONFLICT = "FENCE_CONFLICT"

FENCED_BATCH = """
local function revision_of(key, field)
  local raw = redis.call('HGET', key, field)
  if not raw then return 0 end
  return tonumber(cjson.decode(raw)['r'])
end
if ARGV[1] ~= '' then
  local want = cjson.decode(ARGV[1])
  local raw = redis.call('HGET', KEYS[3], 'lease')
  local lease = cjson.null
  if raw then lease = cjson.decode(raw)['v'] end
  if type(lease) ~= 'table' then return redis.error_reply('FENCE_STALE no controller lease') end
  if (want['owner'] ~= '' and lease['owner'] ~= want['owner'])
    or tonumber(lease['epoch']) ~= tonumber(want['epoch'])
    or tonumber(lease['expires_at']) <= tonumber(want['now_ms']) then
    return redis.error_reply('FENCE_STALE the controller lease moved')
  end
end
local count = #KEYS - 4
for i = 1, count do
  local at = 4 + (i - 1) * 4
  local expected = tonumber(ARGV[at + 2])
  if expected >= 0 and revision_of(KEYS[4 + i], ARGV[at + 1]) ~= expected then
    return redis.error_reply('FENCE_CONFLICT ' .. ARGV[at + 1])
  end
end
local revision = redis.call('INCR', KEYS[1])
for i = 1, count do
  local at = 4 + (i - 1) * 4
  local kind = ARGV[at + 3]
  if kind == 'put' then
    redis.call('HSET', KEYS[4 + i], ARGV[at + 1], '{"r":' .. revision .. ',"v":' .. ARGV[at + 4] .. '}')
  elseif kind == 'delete' then
    redis.call('HDEL', KEYS[4 + i], ARGV[at + 1])
  end
end
redis.call('HSET', KEYS[4], ARGV[4], revision)
redis.call('XADD', KEYS[2], 'MAXLEN', ARGV[2], '*', 'revision', revision, 'owner', ARGV[3], 'records', count)
return revision
"""


def _prefix() -> str:
    from scripts.swarm.store import PREFIX

    return PREFIX


def _decode(raw: str | bytes) -> Record:
    row = json.loads(raw)
    return Record(revision=int(row["r"]), value=row["v"])


def _record(raw: str | bytes | None) -> Record | None:
    return None if raw is None else _decode(raw)


def _fence(lease: LeaseFence | None) -> str:
    if lease is None:
        return ""
    return json.dumps({"owner": lease.owner, "epoch": lease.epoch, "now_ms": lease.now_ms})


def _encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


class RedisRecords:
    """Every fenced record of a swarm lives under keys sharing the swarm's hash tag, so one script sees them all."""

    def __init__(self, redis: Any, prefix: str | None = None, touched: str = "") -> None:
        """touched names a hash each batch stamps with its revision, so transactions watching that hash abort."""
        self.redis = redis
        self.prefix = prefix or _prefix()
        self.touched = touched
        self._script = redis.register_script(FENCED_BATCH)

    def key(self, slug: str, part: str) -> str:
        return f"{self.prefix}:{{{slug}}}:fence:{part}"

    def revision_key(self, slug: str) -> str:
        return self.key(slug, "revision")

    def log_key(self, slug: str) -> str:
        return self.key(slug, "log")

    def layout(self, slug: str) -> str:
        found = _record(self.redis.hget(self.key(slug, META), LAYOUT))
        return found.value if found is not None and isinstance(found.value, str) else ""

    def sections(self, slug: str, names: Sequence[str]) -> dict[str, dict[str, Record]]:
        with self.redis.pipeline(transaction=True) as pipe:
            for name in names:
                pipe.hgetall(self.key(slug, name))
            found = pipe.execute()
        return {name: {field: _decode(raw) for field, raw in found[index].items()} for index, name in enumerate(names)}

    def record(self, slug: str, section: str, key: str) -> Record | None:
        return _record(self.redis.hget(self.key(slug, section), key))

    def batch_call(self, slug: str, batch: FencedBatch) -> tuple[list[str], list[object]]:
        """The keys and arguments the fenced batch script receives, in its order."""
        touched = self.touched or self.key(slug, "touched")
        keys = [self.revision_key(slug), self.log_key(slug), self.key(slug, META), touched]
        owner = batch.lease.owner if batch.lease is not None else ""
        args: list[object] = [_fence(batch.lease), LOG_CAP, owner, f"{slug}:records"]
        for write in batch.writes:
            keys.append(self.key(slug, write.section))
            value = _encode(write.value) if write.kind == "put" else ""
            args.extend([write.key, write.expected, write.kind, value])
        return keys, args

    def queue_batch(self, pipe: Any, slug: str, batch: FencedBatch) -> None:
        """Queues the fenced batch inside a watched transaction; its refusal surfaces when the transaction runs."""
        keys, args = self.batch_call(slug, batch)
        self._script(keys=keys, args=args, client=pipe)

    def move(
        self,
        slug: str,
        watched: Sequence[str],
        plan: Callable[[Any], tuple[Callable[[Any], object], FencedBatch] | None],
    ) -> int | None:
        """Plans under a watch on watched and the record revision, then commits the plan's stage and batch at once.

        Returns the batch size, 0 when the plan declines, or None when a watched key changed before the commit.
        """
        from redis.exceptions import WatchError

        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(*watched, self.revision_key(slug))
                planned = plan(pipe)
                if planned is None:
                    pipe.unwatch()
                    return 0
                stage, batch = planned
                pipe.multi()
                stage(pipe)
                self.queue_batch(pipe, slug, batch)
                pipe.execute()
                return len(batch.writes)
            except WatchError:
                return None

    def fenced_batch(self, slug: str, batch: FencedBatch) -> int:
        """Checks the lease fence and every expected revision, then writes every record or none of them."""
        from redis.exceptions import ResponseError

        keys, args = self.batch_call(slug, batch)
        try:
            return int(self._script(keys=keys, args=args))
        except ResponseError as exc:
            text = str(exc)
            if STALE in text:
                raise StaleLease(text) from exc
            if CONFLICT in text:
                raise RevisionConflict(text) from exc
            raise
