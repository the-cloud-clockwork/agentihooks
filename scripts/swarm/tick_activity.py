from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from scripts.swarm import tick_stop
from scripts.swarm.store import RedisStore


def keep(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    renew()
    store.redis.set(store.key(slug, "last-tick-activity"), clock())


def read(store: RedisStore, slug: str) -> int:
    return int(store.redis.get(store.key(slug, "last-tick-activity")) or 0)


@dataclass
class Renewal:
    """A pass's step hook: before each step it checks for a stop and renews the pass's tick lock, until the pass
    revokes it before releasing that lock. A step worker the pass abandoned keeps a copied context holding this hook,
    so a revoked hook returns quietly and never renews a released token."""

    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]
    lock: Lock = field(default_factory=Lock)
    revoked: bool = False

    def __call__(self) -> None:
        """Check for a stop and renew the tick lock, unless the pass already revoked the hook."""
        with self.lock:
            if self.revoked:
                return
            tick_stop.check()
            keep(self.store, self.slug, self.renew, self.clock)

    def revoke(self) -> None:
        """Stop every later call, from the pass or any worker holding a copy of the hook, from renewing."""
        with self.lock:
            self.revoked = True
