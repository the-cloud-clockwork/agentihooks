from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from scripts.swarm import lease, tick_stop
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
    that is shorter, renews the pass's tick lock, until the pass revokes it before releasing that lock; a revoked hook
    returns quietly."""

    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]
    lock: Lock = field(default_factory=Lock)
    revoked: bool = False
    renewed: int | None = None

    def __call__(self) -> None:
        """Check for a stop, then renew the tick lock unless it was renewed less than one interval ago or the pass
        already revoked the hook; a failed renewal leaves the next call to renew again."""
        with self.lock:
            if self.revoked:
                return
            tick_stop.check()
            at = self.clock()
            if self.renewed is not None and 0 <= at - self.renewed < min(RENEW_MS, lease.tick_ms()):
                return
            keep(self.store, self.slug, self.renew, self.clock)
            self.renewed = at

    def revoke(self) -> None:
        """Stop every later call, from the pass or any worker holding a copy of the hook, from renewing."""
        with self.lock:
            self.revoked = True
