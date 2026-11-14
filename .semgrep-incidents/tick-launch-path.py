from typing import Any


def gated(tick: Any, config: Any, args: tuple[Any, ...]) -> Any:
    if tick.readiness.tick_launches(config.engine):
        # ruleid: tick-launch-path
        return tick.skip_refused(tick._spawn, *args)
    return []


def direct(tick: Any, args: tuple[Any, ...]) -> Any:
    # ruleid: tick-launch-path
    return tick._spawn(*args)


def timed(tick: Any, timing: Any, args: tuple[Any, ...]) -> Any:
    # ruleid: tick-launch-path
    return timing.timed(tick._spawn, *args)


def other(tick: Any, args: tuple[Any, ...]) -> Any:
    # ok: tick-launch-path
    return tick.skip_refused(tick._reap, *args)
