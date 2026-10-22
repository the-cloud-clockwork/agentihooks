import json
import os
import resource
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from scripts.swarm import tick_stop

SWARM = ContextVar("tick_swarm", default=None)
BEFORE_STEP = ContextVar("tick_before_step", default=None)
ON_FAILURE = ContextVar("tick_failure", default=None)
ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("scripts", "hooks", "tests")


def raised_at(exc: BaseException) -> str:
    here = []
    for frame in traceback.extract_tb(exc.__traceback__):
        path = Path(frame.filename).resolve()
        relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else None
        if relative and relative.parts[0] in PACKAGES:
            here.append(f"{relative.as_posix()}:{frame.lineno} {frame.name}")
    return here[-1] if here else ""


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
    except tick_stop.Stopped:
        outcome = "stopped"
        raise
    except BaseException as exc:
        outcome = "error"
        record.update(error=f"{type(exc).__module__}.{type(exc).__qualname__}", raised_at=raised_at(exc))
        if record_failure := ON_FAILURE.get():
            record_failure(name, exc)
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


def keep() -> None:
    before = BEFORE_STEP.get()
    if before is not None:
        before()


def call(function: Callable, *args, **kwargs) -> Any:
    keep()
    with step(f"{function.__module__}.{function.__qualname__}"):
        return function(*args, **kwargs)


def instrument_tick(function: Callable) -> Callable:
    @wraps(function)
    def wrapped(store, slug, *args, **kwargs):
        with tick(slug), step(function.__name__):
            return function(store, slug, *args, **kwargs)

    return wrapped
