from typing import Any


def ungated(tick: Any, config: Any, args: tuple[Any, ...]) -> Any:
    if config.state == "running":
        # ruleid: tick-spawn-without-engine-gate
        return tick.skip_refused(tick._spawn, *args)
    return []


def direct(tick: Any, args: tuple[Any, ...]) -> Any:
    # ruleid: tick-spawn-without-engine-gate
    return tick._spawn(*args)


def gated_after(tick: Any, config: Any, args: tuple[Any, ...]) -> Any:
    # ruleid: tick-spawn-without-engine-gate
    found = tick.skip_refused(tick._spawn, *args)
    if tick.readiness.tick_launches(config.engine):
        return found
    return []


def gated(tick: Any, config: Any, args: tuple[Any, ...]) -> Any:
    if config.state == "running" and tick.readiness.tick_launches(config.engine) and tick.panic.state(None) is None:
        with tick._placement_pass(None):
            # ok: tick-spawn-without-engine-gate
            return tick.skip_refused(tick._spawn, *args)
    return []


def other(tick: Any, args: tuple[Any, ...]) -> Any:
    # ok: tick-spawn-without-engine-gate
    return tick.skip_refused(tick._reap, *args)
