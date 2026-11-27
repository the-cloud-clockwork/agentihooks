from typing import Protocol

from redis import Redis

_PENDING = "return redis.call('ZCOUNT', KEYS[1], ARGV[1], '+inf')"


def key(machine: str) -> str:
    return f"agentihooks:admission:{machine}"


def pending(redis: Redis, machine: str) -> int:
    # ruleid: admission-pending-runs-eval
    return int(redis.eval(_PENDING, 1, key(machine)))


class Pending(Protocol):
    def pending(self, redis: Redis, machine: str) -> int: ...


class Cached(Pending):
    def pending(self, redis: Redis, machine: str) -> int:
        # ruleid: admission-pending-runs-eval
        return int(redis.evalsha("sha", 1, key(machine)))


class Registered(Pending):
    def pending(self, redis: Redis, machine: str) -> int:
        # ruleid: admission-pending-runs-eval
        script = redis.register_script(_PENDING)
        return int(script(keys=[key(machine)]))


class Plain(Pending):
    def pending(self, redis: Redis, machine: str) -> int:
        seconds, micros = redis.time()
        # ok: admission-pending-runs-eval
        return int(redis.zcount(key(machine), f"({seconds * 1000 + micros // 1000}", "+inf"))


def reserve(redis: Redis, machine: str) -> int:
    # ok: admission-pending-runs-eval
    return int(redis.eval(_PENDING, 1, key(machine)))
