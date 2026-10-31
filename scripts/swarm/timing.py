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
from uuid import uuid4

from scripts.swarm import tick_stop

PASS: ContextVar[dict[str, Any] | None] = ContextVar("tick_pass", default=None)
STORE: ContextVar[Any] = ContextVar("tick_store", default=None)
SWARM = ContextVar("tick_swarm", default=None)
BEFORE_STEP = ContextVar("tick_before_step", default=None)
ON_FAILURE = ContextVar("tick_failure", default=None)
AGENT = ContextVar("tick_agent", default=None)
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
def tick(slug: str, store: Any = None) -> Iterator[None]:
    token = SWARM.set(slug)
    recording = STORE.set(store)
    try:
        yield
    finally:
        STORE.reset(recording)
        SWARM.reset(token)


@contextmanager
def acting(name: str) -> Iterator[None]:
    token = AGENT.set(name)
    try:
        yield
    finally:
        AGENT.reset(token)


def append_record(store: Any, slug: str, stream: str, record: dict[str, Any]) -> None:
    from redis.exceptions import RedisError

    try:
        store.redis.xadd(store.key(slug, stream), {"record": json.dumps(record)}, maxlen=50000, approximate=False)
    except RedisError:
        emit(sys.stderr, json.dumps({"event": "swarm_tick_record_failed", "slug": slug, "stream": stream}))


@contextmanager
def pass_record(store: Any, slug: str) -> Iterator[None]:
    if store is None:
        yield
        return
    started = time.perf_counter()
    record = {"pass_id": uuid4().hex, "start_ms": time.time_ns() // 1000000, "failed_steps": [], "outcome": "success"}
    token = PASS.set(record)
    try:
        yield
    except tick_stop.Stopped:
        record["outcome"] = "stopped"
        raise
    except BaseException:
        record["outcome"] = "error"
        raise
    finally:
        try:
            record.update(end_ms=time.time_ns() // 1000000, wall_s=time.perf_counter() - started)
            finish_pass(store, slug, record)
        finally:
            PASS.reset(token)


def finish_pass(store: Any, slug: str, record: dict[str, Any]) -> None:
    from redis.exceptions import RedisError

    try:
        record["agent_count"] = sum(agent.state != "finished" for agent in store.agents(slug))
        append_record(store, slug, "tick:passes", record)
    except RedisError:
        emit(sys.stderr, json.dumps({"event": "swarm_tick_record_failed", "slug": slug, "stream": "tick:passes"}))


@contextmanager
def step(name: str, report: bool = True) -> Iterator[None]:
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
    if (current := PASS.get()) is not None:
        record["pass_id"] = current["pass_id"]
    if (agent := AGENT.get()) is not None:
        record["agent"] = agent
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
        if report and (record_failure := ON_FAILURE.get()):
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
        if outcome != "success" and (current := PASS.get()) is not None:
            current["failed_steps"].append(name)
        if (store := STORE.get()) is not None:
            append_record(store, slug, "tick:steps", record)


def keep() -> None:
    before = BEFORE_STEP.get()
    if before is not None:
        before()


def named(function: Callable) -> str:
    return f"{function.__module__}.{function.__qualname__}"


def call(function: Callable, *args, **kwargs) -> Any:
    keep()
    with step(named(function)):
        return function(*args, **kwargs)


def timed(function: Callable, *args, **kwargs) -> Any:
    with step(named(function), report=False):
        return function(*args, **kwargs)


def instrument_tick(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(store: Any, slug: str, *args: Any, **kwargs: Any) -> Any:
        from scripts.swarm.store import RedisStore

        recording = store if isinstance(store, RedisStore) else None
        with tick(slug, recording), pass_record(recording, slug), step(function.__name__):
            return function(store, slug, *args, **kwargs)

    return wrapped
