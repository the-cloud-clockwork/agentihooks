def frozen(store: RedisStore) -> bool:
    # ruleid: panic-holds-the-fleet
    return panic.state(store) is not None


def dispatch_seat(tick: Any, get: Get) -> Any:
    # ruleid: panic-holds-the-fleet
    if tick.panic.state(get("store")) is not None:
        return []
    return tick.skip_refused(get("slug"))


def watch(store: RedisStore, ok: bool, now: int, report: Report) -> list[str]:
    # ruleid: panic-holds-the-fleet
    return panic.tick_pass(store, ok, now, report)


def press(fixes: list[str]) -> list[str]:
    # ruleid: panic-holds-the-fleet
    return panic.cancel_runs(fixes, panic.REPOS, panic.port)


def sweep(store: RedisStore, fixes: list[str]) -> list[str]:
    # ruleid: panic-holds-the-fleet
    return panic.cancel_runs(fixes, panic.REPOS, panic.port, store=store)


def frozen_for(store: RedisStore, slug: str) -> bool:
    # ok: panic-holds-the-fleet
    return panic.state(store, slug) is not None


def dispatch_seat_for(tick: Any, get: Get) -> Any:
    # ok: panic-holds-the-fleet
    if tick.panic.state(get("store"), get("slug")) is not None:
        return []
    return tick.skip_refused(get("slug"))


def watch_for(store: RedisStore, slug: str, ok: bool, now: int, report: Report) -> list[str]:
    # ok: panic-holds-the-fleet
    return panic.tick_pass(store, slug, ok, now, report)


def press_for(store: RedisStore, slug: str, fixes: list[str]) -> list[str]:
    # ok: panic-holds-the-fleet
    return panic.cancel_runs(fixes, panic.REPOS, panic.port, panic.owner(store, slug))


def sweep_for(store: RedisStore, slug: str, fixes: list[str]) -> list[str]:
    # ok: panic-holds-the-fleet
    return panic.cancel_runs(fixes, panic.REPOS, panic.port, panic.owner(store, slug), (store, slug))
