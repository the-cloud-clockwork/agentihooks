"""The controller replica lock: one replica of a controller Deployment runs passes while the others stand by."""

import sys
import threading
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

REPLICA_ENV = "AGENTIHOOKS_CONTROLLER_REPLICA"
PREFIX = "agentihooks:controller-replica"
LOCK_MS = 30_000
BEAT_S = 10.0
POLL_S = 5.0
KEEP_S = 60.0
NEVER = float("-inf")
BEAT_THREAD = "controller-replica-beat"
KEEP_THREAD = "controller-credential-keep"
TAKE = """
local held = redis.call('GET', KEYS[1])
if held and held ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return 1
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class ReplicaLock:
    """Hold the one lock that lets a controller replica of hive ``hive`` run passes."""

    def __init__(
        self, redis: Any, hive: str, owner: str, lock_ms: int = LOCK_MS, clock: Callable[[], float] = monotonic
    ) -> None:
        self.redis, self.owner, self.lock_ms, self.clock = redis, owner, lock_ms, clock
        self.key = f"{PREFIX}:{hive}"
        self.taken_at = NEVER

    def take(self) -> bool:
        """Take or renew the lock; answer False while another replica holds it."""
        taken = bool(self.redis.eval(TAKE, 1, self.key, self.owner, self.lock_ms))
        if taken:
            self.taken_at = self.clock()
        return taken

    def fresh(self, margin_s: float) -> bool:
        """Tell whether the last successful take still holds the lock ``margin_s`` past now."""
        return self.clock() - self.taken_at < self.lock_ms / 1000 - margin_s

    def release(self) -> bool:
        """Drop the lock when this replica holds it, so a standby takes over at its next poll."""
        return bool(self.redis.eval(RELEASE, 1, self.key, self.owner))

    def holder(self) -> str:
        """Name the replica holding the lock, or an empty string."""
        return self.redis.get(self.key) or ""

    def lead(self, wait: Callable[[float], None], poll_s: float = POLL_S) -> None:
        """Stand by until this replica takes the lock."""
        while not self.take():
            wait(poll_s)

    def beat(self, lost: Callable[[], None], stop: threading.Event, beat_s: float = BEAT_S) -> threading.Thread:
        """Renew the lock every ``beat_s`` until ``stop`` is set; call ``lost`` once if another replica holds it,
        or once Redis has refused renewals for so long that the lock could expire before the next beat."""

        def renew() -> bool:
            from redis.exceptions import RedisError

            try:
                kept = self.take()
            except RedisError:
                kept = self.fresh(beat_s)
            if not kept:
                lost()
            return kept

        return _repeat(renew, stop, beat_s, BEAT_THREAD)


def keep(action: Callable[[], None], stop: threading.Event, every_s: float = KEEP_S) -> threading.Thread:
    """Run ``action`` every ``every_s`` until ``stop`` is set, reporting a failed run and trying again."""

    def run() -> bool:
        try:
            action()
        except Exception as exc:
            print(f"controller: keeping the controller credential failed: {exc}", file=sys.stderr, flush=True)
        return True

    return _repeat(run, stop, every_s, KEEP_THREAD)


def _repeat(action: Callable[[], bool], stop: threading.Event, every_s: float, name: str) -> threading.Thread:
    def run() -> None:
        while not stop.wait(every_s) and action():
            pass

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def configured(environ: Mapping[str, str], redis: Any, hive: str) -> ReplicaLock | None:
    """Build the replica lock of a controller whose Deployment names its replica, else None."""
    owner = environ.get(REPLICA_ENV, "")
    return ReplicaLock(redis, hive, owner) if owner else None
