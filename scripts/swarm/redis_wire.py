"""The Redis commands a redis-py call sends, found by making the call on a client that records instead of sending."""

import inspect
import itertools
from functools import cache

import redis
from redis.client import Pipeline, PubSub
from redis.lock import Lock

LUA = "redis.call("
SAMPLES = ("k", 1, ["k"], {"k": "v"})
SPARE = 3
REFUSED = (TypeError, ValueError, AttributeError, redis.RedisError)


class _Recorder(redis.Redis):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def execute_command(self, *args, **options):
        name = str(args[0]).upper()
        self.sent.append(name)
        if name.endswith("SCAN"):
            return 0, []
        return 1 if name in ("SET", "EVALSHA") else None


def lua(text: str) -> set[str]:
    """The commands a Lua script in the text calls through redis.call."""
    found = set()
    for part in text.split(LUA)[1:]:
        quoted = part.lstrip()
        if quoted[:1] in ("'", '"') and (end := quoted.find(quoted[0], 1)) > 1:
            found.add(quoted[1:end].upper())
    return found


def _target(owner: type, recorder: redis.Redis):
    if owner is Pipeline:
        return recorder.pipeline(transaction=False)
    return recorder.pubsub() if owner is PubSub else recorder


def _record(owner: type, name: str, values: tuple) -> list[str]:
    recorder = _Recorder()
    target = _target(owner, recorder)
    try:
        result = getattr(target, name)(*values)
        if inspect.isgenerator(result):
            list(result)
    except REFUSED:
        pass
    if owner is Pipeline:
        recorder.sent.extend(str(args[0]).upper() for args, _ in target.command_stack)
    return recorder.sent


@cache
def wire(owner: type, name: str) -> frozenset[str]:
    """The commands a redis-py method sends; empty for a method that sends none, such as pipeline or close."""
    params = list(inspect.signature(getattr(owner, name)).parameters.values())[1:]
    params = [p for p in params if p.kind is p.POSITIONAL_OR_KEYWORD]
    required = sum(1 for p in params if p.default is p.empty)
    for width in range(required, min(len(params), required + SPARE) + 1):
        for values in itertools.product(SAMPLES, repeat=width):
            if sent := _record(owner, name, values):
                return frozenset(sent)
    return frozenset()


@cache
def _lock() -> frozenset[str]:
    recorder = _Recorder()
    lock = recorder.lock("k", timeout=1)
    lock.acquire(blocking=False)
    lock.extend(1)
    lock.reacquire()
    lock.release()
    scripts = [value for name, value in vars(Lock).items() if name.startswith("LUA_")]
    return frozenset(recorder.sent).union({"SCRIPT LOAD"}, *map(lua, scripts))


HELPERS = {
    "lock": _lock,
    "register_script": lambda: frozenset({"EVALSHA", "SCRIPT LOAD"}),
    "transaction": lambda: frozenset({"WATCH", "MULTI", "EXEC"}),
    "execute": lambda: frozenset({"MULTI", "EXEC"}),
}


@cache
def methods(owner: type) -> frozenset[str]:
    return frozenset(name for name in dir(owner) if not name.startswith("_") and callable(getattr(owner, name)))
