import json
import os
import resource
import sys
import time
import traceback
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.swarm import tick_stop
from scripts.tick import metrics as tick_metrics

HISTORY_LIMIT = 50000
ABANDONED = "abandoned"
PASS: ContextVar[dict[str, Any] | None] = ContextVar("tick_pass", default=None)
STORE: ContextVar[Any] = ContextVar("tick_store", default=None)
SWARM: ContextVar[str | None] = ContextVar("tick_swarm", default=None)
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
        store.redis.xadd(
            store.key(slug, stream), {"record": json.dumps(record)}, maxlen=HISTORY_LIMIT, approximate=False
        )
    except RedisError:
        emit(sys.stderr, json.dumps({"event": "swarm_tick_record_failed", "slug": slug, "stream": stream}))


WALLS = "tick:walls:"
WINDOW = 60


def append_step(store: Any, slug: str, record: dict[str, Any]) -> None:
    """Append a step ``record`` to the steps stream and, for a budgeted step, push its wall to the step's window of
    the last ``WINDOW`` walls in the same pipeline."""
    if "budget_s" not in record or "wall_s" not in record:
        append_record(store, slug, "tick:steps", record)
        return
    from redis.exceptions import RedisError

    walls = store.key(slug, WALLS + record["step"])
    try:
        pipe = store.redis.pipeline(transaction=False)
        pipe.xadd(
            store.key(slug, "tick:steps"), {"record": json.dumps(record)}, maxlen=HISTORY_LIMIT, approximate=False
        )
        pipe.lpush(walls, repr(record["wall_s"]))
        pipe.ltrim(walls, 0, WINDOW - 1)
        pipe.execute()
    except RedisError:
        emit(sys.stderr, json.dumps({"event": "swarm_tick_record_failed", "slug": slug, "stream": "tick:steps"}))


STEP_RAN = "tick:step-ran"


def step_ends(store: Any, slug: str) -> dict[str, float]:
    """Return the end time of each step's last ok or failed run for swarm ``slug``, empty when Redis fails."""
    from redis.exceptions import RedisError

    try:
        found = store.redis.hgetall(store.key(slug, STEP_RAN))
    except RedisError:
        return {}
    return {name: float(ended) for name, ended in found.items()}


def step_ended(store: Any, slug: str, name: str, ended: float) -> None:
    """Record ``ended`` as the end time of step ``name``'s last run for swarm ``slug``."""
    from redis.exceptions import RedisError

    try:
        store.redis.hset(store.key(slug, STEP_RAN), name, repr(ended))
    except RedisError:
        emit(sys.stderr, json.dumps({"event": "swarm_tick_record_failed", "slug": slug, "stream": STEP_RAN}))


@contextmanager
def pass_record(store: Any, slug: str) -> Generator[None, None, None]:
    if store is None:
        yield
        return
    started = time.perf_counter()
    record: dict[str, Any] = {
        "pass_id": uuid4().hex,
        "start_ms": time.time_ns() // 1000000,
        "failed_steps": [],
        "outcome": "success",
    }
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
            tick_metrics.pass_finished(slug, record)
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
def step(name: str, report: bool = True) -> Iterator[dict[str, Any] | None]:
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
        yield record
    except tick_stop.Stopped:
        outcome = "stopped"
        raise
    except BaseException as exc:
        waits = getattr(exc, "waiting", None)
        outcome = "waiting" if callable(waits) and waits() else "error"
        record.update(error=f"{type(exc).__module__}.{type(exc).__qualname__}", raised_at=raised_at(exc))
        record.update(getattr(exc, "step_fields", dict)())
        if outcome == "error" and report and (record_failure := ON_FAILURE.get()):
            record_failure(name, exc)
        raise
    finally:
        ended = time.monotonic()
        own_end = resource.getrusage(resource.RUSAGE_THREAD)
        child_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        outcome = record.get("outcome", outcome)
        record.update(
            phase="finished",
            outcome=outcome,
            wall_s=ended - started,
            own_cpu_s=(own_end.ru_utime - own.ru_utime) + (own_end.ru_stime - own.ru_stime),
            reaped_child_cpu_s=(child_end.ru_utime - child.ru_utime) + (child_end.ru_stime - child.ru_stime),
        )
        emit(sys.stderr, json.dumps(record))
        tick_metrics.step_finished(slug, record)
        if outcome not in ("success", "waiting", ABANDONED) and (current := PASS.get()) is not None:
            current["failed_steps"].append(name)
        if (store := STORE.get()) is not None:
            append_step(store, slug, record)


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


def within(
    budget_s: float,
    function: Callable[..., Any],
    *args: Any,
    lane: str | None = None,
    name: str | None = None,
    on_end: Callable[[], bool] | None = None,
) -> Any:
    """Run ``function`` as a timed step recorded as ``name``, else as the function's own name, whose record carries
    ``budget_s``, its ``lane`` when given, and reads late when it overruns; ``on_end`` is called once as the run
    ends and returns whether the run was already given up at its budget, which makes the record read abandoned."""
    keep()
    with step(name or named(function)) as record:
        if record is not None:
            record["budget_s"] = budget_s
            if lane is not None:
                record["lane"] = lane
        started = time.monotonic()
        try:
            result = function(*args)
        finally:
            if on_end is not None and on_end() and record is not None:
                record["outcome"] = ABANDONED
        if record is not None:
            record["late"] = time.monotonic() - started > budget_s
        return result


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
