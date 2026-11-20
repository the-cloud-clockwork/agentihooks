import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from scripts.swarm import lease, tick_stop, timing
from scripts.swarm.store import RedisStore

RENEW_MS = 10_000


def keep(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    renew()
    store.redis.set(store.key(slug, "last-tick-activity"), clock())


def read(store: RedisStore, slug: str) -> int:
    return int(store.redis.get(store.key(slug, "last-tick-activity")) or 0)


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
        already revoked the hook; a failed renewal leaves the next call to renew again. A call that renews, or finds
        another call holding the hook, journals its wait, its hold and its renewal's Redis time."""
        asked = time.monotonic()
        blocked = not self.lock.acquire(blocking=False)
        if blocked:
            self.lock.acquire()
        held = time.monotonic()
        renewing: float | None = None
        try:
            if self.revoked:
                return
            tick_stop.check()
            at = self.clock()
            if self.renewed is not None and 0 <= at - self.renewed < interval_ms:
                return
            renewing = time.monotonic()
            keep(self.store, self.slug, self.renew, self.clock)
            self.renewed = at
        finally:
            ended = time.monotonic()
            self.lock.release()
            if blocked or renewing is not None:
                _journal(self.slug, blocked, (asked, held, ended), renewing)

    def revoke(self) -> None:
        """Stop every later call, from the pass or any worker holding a copy of the hook, from renewing."""
        with self.lock:
            self.revoked = True


def _journal(slug: str, blocked: bool, times: tuple[float, float, float], renewing: float | None) -> None:
    """Journal one ``swarm_tick_lock_renewal`` line for a step hook call that asked for the hook, held it and let it
    go at ``times``: whether another call held it, its wait for it, how long it held it and, when it renewed from
    ``renewing``, the renewal's Redis round trips."""
    asked, held, ended = times
    line: dict[str, Any] = {
        "event": "swarm_tick_lock_renewal",
        "slug": slug,
        "blocked": blocked,
        "renewed": renewing is not None,
        "wait_s": held - asked,
        "wall_s": ended - held,
    }
    if renewing is not None:
        line["redis_s"] = ended - renewing
    if (current := timing.PASS.get()) is not None:
        line["pass_id"] = current["pass_id"]
    timing.emit(sys.stderr, json.dumps(line))  # pyright: ignore[reportUnknownMemberType]


def renew_now() -> None:
    """Run the pass's step hook at once, past its renewal interval, so a write that follows runs only while the pass
    still holds its tick lock and lease; outside a pass it does nothing."""
    hook: Callable[[], None] | None = timing.BEFORE_STEP.get()
    if hook is not None:
        getattr(hook, "now", hook)()
