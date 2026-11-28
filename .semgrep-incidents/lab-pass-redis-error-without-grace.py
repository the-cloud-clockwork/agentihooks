from collections.abc import Callable
from typing import Any

from redis import RedisError

from scripts.swarm import redis_grace

Clock = Callable[[], float]
Sleep = Callable[[float], None]


def skips_forever(scheduler: Any, passes: int, clock: Clock, sleep: Sleep) -> int:
    done = 0
    while passes == 0 or done < passes:
        # ruleid: lab-pass-redis-error-without-grace
        try:
            for action in scheduler.tick(int(clock() * 1000)):
                print(action, flush=True)
        except RedisError as error:
            print(f"pass {done + 1} skipped on {type(error).__name__}: {error}", flush=True)
        done += 1
        sleep(1.0)
    return 0


def resets_but_never_asks(scheduler: Any, passes: int, clock: Clock, sleep: Sleep, grace_s: float) -> int:
    outage = redis_grace.Outage(grace_s, clock)
    done = 0
    while passes == 0 or done < passes:
        # ruleid: lab-pass-redis-error-without-grace
        try:
            for action in scheduler.tick(int(clock() * 1000)):
                print(action, flush=True)
            outage.ended()
        except RedisError:
            print(f"pass {done + 1} skipped", flush=True)
        done += 1
        sleep(1.0)
    return 0


def exits_past_the_window(scheduler: Any, passes: int, clock: Clock, sleep: Sleep, grace_s: float) -> int:
    outage = redis_grace.Outage(grace_s, clock)
    done = 0
    while passes == 0 or done < passes:
        # ok: lab-pass-redis-error-without-grace
        try:
            for action in scheduler.tick(int(clock() * 1000)):
                print(action, flush=True)
            outage.ended()
        except RedisError as error:
            if not outage.tolerated():
                print(f"pass {done + 1} ends the scheduler past the {grace_s:g}s grace window", flush=True)
                return 1
            print(f"pass {done + 1} skipped on {type(error).__name__}: {error}", flush=True)
        done += 1
        sleep(1.0)
    return 0


def keeps_the_answer(scheduler: Any, passes: int, clock: Clock, sleep: Sleep, grace_s: float) -> int:
    outage = redis_grace.Outage(grace_s, clock)
    done = 0
    while passes == 0 or done < passes:
        # ok: lab-pass-redis-error-without-grace
        try:
            scheduler.tick(int(clock() * 1000))
            outage.ended()
        except RedisError:
            lapsed = not outage.tolerated()
            if lapsed:
                return 1
        done += 1
        sleep(1.0)
    return 0
