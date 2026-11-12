from collections.abc import Callable

from scripts.swarm import tick_activity, timing
from scripts.swarm.store import RedisStore


def closure_hook(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    def before_step() -> None:
        tick_activity.keep(store, slug, renew, clock)

    # ruleid: pass-step-hook-unrevoked
    timing.BEFORE_STEP.set(before_step)


def lambda_hook(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    # ruleid: pass-step-hook-unrevoked
    timing.BEFORE_STEP.set(lambda: tick_activity.keep(store, slug, renew, clock))


def revocable_hook(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    renewal = tick_activity.Renewal(store, slug, renew, clock)
    # ok: pass-step-hook-unrevoked
    keeping = timing.BEFORE_STEP.set(renewal)
    renewal.revoke()
    timing.BEFORE_STEP.reset(keeping)


def inline_hook(store: RedisStore, slug: str, renew: Callable[[], None], clock: Callable[[], int]) -> None:
    # ok: pass-step-hook-unrevoked
    timing.BEFORE_STEP.set(tick_activity.Renewal(store, slug, renew, clock))
