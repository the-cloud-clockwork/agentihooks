from collections.abc import Callable
from dataclasses import dataclass

from scripts.swarm import controller, tick_activity, tick_stop
from scripts.swarm.lease import Lease
from scripts.swarm.store import RedisStore
from scripts.swarm.tick_activity import RENEW_MS, keep


@dataclass
class EveryStep:
    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]

    def __call__(self) -> None:
        tick_stop.check()
        # ruleid: pass-step-renewal-unthrottled
        keep(self.store, self.slug, self.renew, self.clock)


@dataclass
class EveryStepByModule:
    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]

    def __call__(self) -> None:
        # ruleid: pass-step-renewal-unthrottled
        tick_activity.keep(self.store, self.slug, self.renew, self.clock)


@dataclass
class EveryStepLock:
    store: RedisStore
    slug: str
    held: Lease
    token: str

    def __call__(self) -> None:
        # ruleid: pass-step-renewal-unthrottled
        controller.keep_tick(self.store, self.slug, self.held, self.token, 600_000, "pass step")


@dataclass
class EveryStepThroughHelper:
    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]

    def __call__(self) -> None:
        self._keep()

    def _keep(self) -> None:
        tick_stop.check()
        # ruleid: pass-step-renewal-unthrottled
        keep(self.store, self.slug, self.renew, self.clock)


@dataclass
class OncePerInterval:
    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]
    renewed: int | None = None

    def __call__(self) -> None:
        self._keep(RENEW_MS)

    def now(self) -> None:
        self._keep(0)

    def _keep(self, interval_ms: int) -> None:
        tick_stop.check()
        at = self.clock()
        if self.renewed is not None and 0 <= at - self.renewed < interval_ms:
            return
        # ok: pass-step-renewal-unthrottled
        keep(self.store, self.slug, self.renew, self.clock)
        self.renewed = at


@dataclass
class DueOnly:
    store: RedisStore
    slug: str
    renew: Callable[[], None]
    clock: Callable[[], int]
    renewed: int = 0

    def __call__(self) -> None:
        if self.clock() - self.renewed >= tick_activity.RENEW_MS:
            # ok: pass-step-renewal-unthrottled
            tick_activity.keep(self.store, self.slug, self.renew, self.clock)
            self.renewed = self.clock()


def renew_pass(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    # ok: pass-step-renewal-unthrottled
    keep(store, slug, renew, clock)
