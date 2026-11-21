from collections.abc import Callable
from typing import Any

from redis import RedisError

Clock = Callable[[], float]
Sleep = Callable[[float], None]


def unguarded(scheduler: Any, relaying: Any, passes: int, clock: Clock, sleep: Sleep) -> int:
    done = 0
    while passes == 0 or done < passes:
        # ruleid: lab-pass-uncaught-redis-error
        for action in scheduler.tick(int(clock() * 1000)):
            print(action, flush=True)
        # ruleid: lab-pass-uncaught-redis-error
        for action in relaying.tick():
            print(action, flush=True)
        done += 1
        sleep(1.0)
    return 0


def guards_only_timeouts(scheduler: Any, passes: int, clock: Clock, sleep: Sleep) -> int:
    done = 0
    while passes == 0 or done < passes:
        try:
            # ruleid: lab-pass-uncaught-redis-error
            for action in scheduler.tick(int(clock() * 1000)):
                print(action, flush=True)
        except TimeoutError as error:
            print(f"pass {done + 1} skipped on {error}", flush=True)
        done += 1
        sleep(1.0)
    return 0


def guarded(scheduler: Any, relaying: Any, passes: int, clock: Clock, sleep: Sleep) -> int:
    done = 0
    while passes == 0 or done < passes:
        try:
            # ok: lab-pass-uncaught-redis-error
            for action in scheduler.tick(int(clock() * 1000)):
                print(action, flush=True)
            # ok: lab-pass-uncaught-redis-error
            for action in relaying.tick():
                print(action, flush=True)
        except RedisError as error:
            print(f"pass {done + 1} skipped on {type(error).__name__}: {error}", flush=True)
        done += 1
        sleep(1.0)
    return 0


def one_pass(scheduler: Any, clock: Clock) -> list[str]:
    # ok: lab-pass-uncaught-redis-error
    return scheduler.tick(int(clock() * 1000))
