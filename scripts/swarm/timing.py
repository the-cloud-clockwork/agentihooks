import json
import os
import resource
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

SWARM = ContextVar("tick_swarm", default=None)


def emit(stream, line: str) -> None:
    stream.write(line + "\n")
    stream.flush()


@contextmanager
def tick(slug: str) -> Iterator[None]:
    token = SWARM.set(slug)
    try:
        yield
    finally:
        SWARM.reset(token)


@contextmanager
def step(name: str) -> Iterator[None]:
    slug = SWARM.get()
    if slug is None:
        yield
        return
    started = time.monotonic()
    own = resource.getrusage(resource.RUSAGE_THREAD)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    record = {
        "event": "swarm_tick_step",
        "phase": "started",
        "pid": os.getpid(),
        "slug": slug,
        "step": name,
        "started": started,
    }
    emit(sys.stderr, json.dumps(record))
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        ended = time.monotonic()
        own_end = resource.getrusage(resource.RUSAGE_THREAD)
        child_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        record.update(
            phase="finished",
            outcome=outcome,
            wall_s=ended - started,
            own_cpu_s=(own_end.ru_utime - own.ru_utime) + (own_end.ru_stime - own.ru_stime),
            reaped_child_cpu_s=(child_end.ru_utime - child.ru_utime) + (child_end.ru_stime - child.ru_stime),
        )
        emit(sys.stderr, json.dumps(record))


def call(function: Callable, *args, **kwargs) -> Any:
    with step(f"{function.__module__}.{function.__qualname__}"):
        return function(*args, **kwargs)


def instrument_tick(function: Callable) -> Callable:
    @wraps(function)
    def wrapped(store, slug, *args, **kwargs):
        with tick(slug), step(function.__name__):
            return function(store, slug, *args, **kwargs)

    return wrapped
