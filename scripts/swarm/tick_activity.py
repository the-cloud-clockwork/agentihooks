import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from scripts.swarm import lease, tick_stop, timing
from scripts.swarm.store import RedisStore

RENEW_MS = 10_000


def keep(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    renew()
    store.redis.set(store.key(slug, "last-tick-activity"), clock())


def read(store: RedisStore, slug: str) -> int:
    return int(store.redis.get(store.key(slug, "last-tick-activity")) or 0)


def journal(slug: str, blocked: bool, renewed: bool, wait_s: float, wall_s: float, redis_s: float | None) -> None:
    """Journal one ``swarm_tick_lock_renewal`` line: whether another call held the hook and whether the renewal
    succeeded, the seconds the call waited for the hook lock and held it, and the seconds its renewal's Redis calls
    took, failed or not, or null when it tried none."""
    current = timing.PASS.get()
    line = {
        "event": "swarm_tick_lock_renewal",
        "slug": slug,
        "pass_id": None if current is None else current["pass_id"],
        "blocked": blocked,
        "renewed": renewed,
        "wait_s": wait_s,
        "wall_s": wall_s,
        "redis_s": redis_s,
    }
    print(json.dumps(line), file=sys.stderr, flush=True)


@dataclass
class Renewal:
    """A pass's step hook: before each step it checks for a stop and, at most once per RENEW_MS or per lease tick when
    that is shorter, renews the pass's tick lock, while a lease check before a write renews at once; until the pass
    revokes it before releasing that lock, after which every call returns quietly."""

    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]
    lock: Lock = field(default_factory=Lock)
    revoked: bool = False
    renewed: int | None = None

    def __call__(self) -> None:
        """Check for a stop, then renew the tick lock unless it was renewed less than one interval ago."""
        self._keep(min(RENEW_MS, lease.tick_ms()))

    def now(self) -> None:
        """Check for a stop and renew the tick lock whenever it was last renewed."""
        self._keep(0)

    def _keep(self, interval_ms: int) -> None:
        """Check for a stop and renew the tick lock unless its last renewal is under ``interval_ms`` old or the pass
        already revoked the hook; a failed renewal leaves the next call to renew again. A call that tried a renewal,
        or that found another call holding the hook, journals one ``swarm_tick_lock_renewal`` line."""
        asked = time.monotonic()
        blocked = self.lock.locked()
        with self.lock:
            held = time.monotonic()
            started: float | None = None
            renewed = False
            try:
                if self.revoked:
                    return
                tick_stop.check()
                at = self.clock()
                if self.renewed is not None and 0 <= at - self.renewed < interval_ms:
                    return
                started = time.monotonic()
                keep(self.store, self.slug, self.renew, self.clock)
                self.renewed = at
                renewed = True
            finally:
                ended = time.monotonic()
                if blocked or started is not None:
                    redis_s = None if started is None else ended - started
                    journal(self.slug, blocked, renewed, held - asked, ended - held, redis_s)

    def revoke(self) -> None:
        """Stop every later call, from the pass or any worker holding a copy of the hook, from renewing."""
        with self.lock:
            self.revoked = True


def renew_now() -> None:
    """Run the pass's step hook at once, past its renewal interval, so a write that follows runs only while the pass
    still holds its tick lock and lease; outside a pass it does nothing."""
    hook: Callable[[], None] | None = timing.BEFORE_STEP.get()
    if hook is not None:
        getattr(hook, "now", hook)()
