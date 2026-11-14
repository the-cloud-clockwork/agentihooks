import sys


class RedisError(Exception):
    pass


async def deliver() -> None:
    return None


async def pause(seconds: float) -> None:
    del seconds


async def _push_dies() -> None:
    while True:
        # ruleid: push-loop-ends-on-non-redis-error
        try:
            await deliver()
        except RedisError:
            await pause(1.0)


async def _push_kept() -> None:
    while True:
        # ok: push-loop-ends-on-non-redis-error
        try:
            await deliver()
        except Exception as exc:
            print(f"push pass failed ({exc})", file=sys.stderr)
            await pause(1.0)


async def _push_bare() -> None:
    while True:
        # ruleid: push-loop-ends-on-non-redis-error
        try:
            await deliver()
        except:  # noqa: E722
            await pause(1.0)
