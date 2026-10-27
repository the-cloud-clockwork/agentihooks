import functools


@functools.cache
def doubled(value: int) -> int:
    return value * 2
