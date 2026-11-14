from scripts.swarm import fleet_red


def live_snapshot(store: object, slug: str) -> object:
    # ruleid: fleet-red-checker-without-saved-reader
    return fleet_red.checker()


def tick_view(tick: object, store: object, slug: str) -> object:
    # ruleid: fleet-red-checker-without-saved-reader
    return tick.fleet_red.checker()


def live_reader_snapshot(store: object, slug: str) -> object:
    # ruleid: fleet-red-checker-without-saved-reader
    return fleet_red.checker(fleet_red.read)


def clocked_live_snapshot(store: object, slug: str, clock: object) -> object:
    # ruleid: fleet-red-checker-without-saved-reader
    return fleet_red.checker(clock=clock)


def saved_snapshot(store: object, slug: str) -> object:
    # ok: fleet-red-checker-without-saved-reader
    return fleet_red.checker(fleet_red.saved_reader(store, slug))


def saved_tick_view(tick: object, store: object, slug: str) -> object:
    # ok: fleet-red-checker-without-saved-reader
    return tick.fleet_red.checker(tick.fleet_red.saved_reader(store, slug))


def saved_clocked_snapshot(store: object, slug: str, clock: object) -> object:
    # ok: fleet-red-checker-without-saved-reader
    return fleet_red.checker(fleet_red.saved_reader(store, slug), clock)
