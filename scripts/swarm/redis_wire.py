"""The Redis commands a redis-py call sends, found by making the call on a client that records instead of sending."""

import inspect
import itertools
from functools import cache

import redis
from redis.client import PubSub
from redis.lock import Lock

LUA = "redis.call("
SAMPLES = ("k", 1, ["k"], {"k": "v"})
SPARE = 3
REFUSED = (TypeError, ValueError, AttributeError, IndexError, redis.RedisError)


class _Recorder(redis.Redis):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def execute_command(self, *args, **options):
        name = str(args[0]).upper()
        self.sent.append(name)
        return True if name in ("SET", "EVALSHA") else None


class _Subscriber(PubSub):
    def __init__(self, recorder: _Recorder):
        super().__init__(recorder.connection_pool)
        self.recorder = recorder

    def execute_command(self, *args):
        self.recorder.sent.append(str(args[0]).upper())


def lua(text: str) -> set[str]:
    """The commands a Lua script in the text calls through redis.call."""
    found = set()
    for part in text.split(LUA)[1:]:
        quoted = part.lstrip()
        if quoted[:1] in ("'", '"'):
            found.add(quoted[1:].partition(quoted[0])[0].upper())
    return found


def _record(owner: type, name: str, values: tuple) -> tuple[list[str], bool]:
    recorder = _Recorder()
    target = _Subscriber(recorder) if owner is PubSub else recorder
    try:
        result = getattr(target, name)(*values)
        if inspect.isgenerator(result):
            list(result)
    except REFUSED:
        return recorder.sent, False
    return recorder.sent, True


@cache
def wire(owner: type, name: str) -> frozenset[str]:
    """The commands a redis-py method sends; empty for a class method or for a method that completed sending none,
    such as pipeline or close. A method no sample call completes is refused, so it never drops out silently."""
    if isinstance(inspect.getattr_static(owner, name), (classmethod, staticmethod)):
        return frozenset()
    params = list(inspect.signature(getattr(owner, name)).parameters.values())[1:]
    params = [p for p in params if p.kind is p.POSITIONAL_OR_KEYWORD]
    required = sum(1 for p in params if p.default is p.empty)
    completed = False
    for width in range(required, min(len(params), required + SPARE) + 1):
        for values in itertools.product(SAMPLES, repeat=width):
            sent, finished = _record(owner, name, values)
            if sent:
                return frozenset(sent)
            completed = completed or finished
    if completed:
        return frozenset()
    raise LookupError(f"no sample call of {owner.__name__}.{name} completed, so its Redis command is unknown")


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
