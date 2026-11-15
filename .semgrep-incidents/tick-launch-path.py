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


def runtime_spawn(tick: Any, config: Any, task: dict[str, Any]) -> Any:
    # ruleid: tick-launch-path
    return tick.runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task)


def fetched_runtime_spawn(get: Any, config: Any, task: dict[str, Any]) -> Any:
    # ruleid: tick-launch-path
    return get("runtime").spawn(config, "eng", "engineer@a1b2c3-0001", task)


def gated_runtime_spawn(tick: Any, runtime: Any, args: tuple[Any, ...]) -> Any:
    # ruleid: tick-launch-path
    return tick.skip_refused(runtime.spawn, *args)


def routed_spawn(router: Any, request: Any) -> Any:
    # ruleid: tick-launch-path
    return router.spawn(request)


def seat_place(tick: Any, args: tuple[Any, ...]) -> Any:
    # ok: tick-launch-path
    return tick.seat_spawn.place(*args)


def pool_spawn(pool: Any, job: Any) -> Any:
    # ok: tick-launch-path
    return pool.spawn(job)
