import argparse
import json
import math
import sqlite3
import threading
import time
from _thread import LockType
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

MAX_SAMPLES = 50000
RETENTION_SECONDS = 7 * 86400
SLOW_SECONDS = 5.0
CURRENT = ContextVar("ledger_request_timing", default=None)
SAMPLES = deque(maxlen=MAX_SAMPLES)
GUARD = threading.Lock()
STARTED_AT = time.time()
EVICTED_THROUGH = float("-inf")


@dataclass
class Timing:
    lock_wait: float = 0.0


RESOURCES = frozenset(
    (
        "metadata",
        "threads",
        "counts",
        "sources",
        "tasks",
        "phases",
        "plans",
        "slices",
        "notes",
        "chat",
        "questions",
        "followups",
        "priorities",
        "notifications",
        "artifacts",
        "artifact_trash",
        "alerts",
        "members",
        "events",
        "operations",
        "export",
        "swarm",
        "workspace",
        "media",
        "agent-token",
        "hierarchy",
        "freezes",
    )
)
FIXED_ROUTES = frozenset(
    (
        "/",
        "/healthz",
        "/logo.png",
        "/favicon.ico",
        "/api/layout",
        "/api/bin",
        "/api/v1/metrics",
        "/api/v1/ledgers",
        "/api/v1/bin",
        "/api/v1/bin/actions",
        "/api/v1/layout",
        "/api/v1/routing/settings",
        "/api/v1/proof-ledgers",
    )
)
PREFIX_ROUTES = (
    ("/static/", "/static/:asset"),
    ("/media/", "/media/:slug/:item"),
    ("/artifacts/", "/artifacts/:slug/:item"),
    ("/api/media/", "/api/media/:slug"),
    ("/api/artifacts/", "/api/artifacts/:slug"),
    ("/api/v1/proof-ledgers/", "/api/v1/proof-ledgers/:slug/remove"),
    ("/api/v1/bin/", "/api/v1/bin/:slug"),
    ("/api/", "/api/:slug"),
)


def ledger_route(parts: list[str]) -> str:
    resource = parts[0] if parts else "metadata"
    prefix = "/api/v1/ledgers/:slug"
    if resource not in RESOURCES:
        return prefix + "/:resource"
    route = f"{prefix}/{resource}"
    if len(parts) > 1:
        route += "/:item"
    if len(parts) > 2:
        route += "/" + (parts[2] if parts[2] in {"comments", "answers", "workspace"} else ":resource")
    return route


def route_name(path: str) -> str:
    path = path.split("?", 1)[0]
    parts = path.strip("/").split("/")
    if parts[:3] == ["api", "v1", "ledgers"] and len(parts) > 3:
        return ledger_route(parts[4:])
    if parts[:2] == ["api", "swarm"]:
        return "/api/swarm/:slug" + ("/models" if parts[3:] == ["models"] else "")
    if path in FIXED_ROUTES:
        return path
    for prefix, route in PREFIX_ROUTES:
        if path.startswith(prefix):
            return route
    return "/:slug"


def record(method: str, route: str, wall: float, cpu: float, lock_wait: float) -> None:
    global EVICTED_THROUGH

    with GUARD:
        ended = time.time()
        if len(SAMPLES) == SAMPLES.maxlen:
            EVICTED_THROUGH = max(EVICTED_THROUGH, SAMPLES[0][0])
        SAMPLES.append((ended, method, route, wall, cpu, lock_wait))
        while SAMPLES and SAMPLES[0][0] < ended - RETENTION_SECONDS:
            expired = SAMPLES.popleft()
            EVICTED_THROUGH = max(EVICTED_THROUGH, expired[0])


def measured(method: Callable) -> Callable:
    @wraps(method)
    def request(handler):
        if CURRENT.get() is not None:
            return method(handler)
        timing = Timing()
        token = CURRENT.set(timing)
        wall, cpu = time.perf_counter(), time.thread_time()
        try:
            return method(handler)
        finally:
            elapsed, used = time.perf_counter() - wall, time.thread_time() - cpu
            CURRENT.reset(token)
            if method.__name__ != "handle_one_request" or handler.raw_requestline:
                name = (
                    method.__name__.removeprefix("do_")
                    if method.__name__.startswith("do_")
                    else handler.command or "OTHER"
                )
                name = (
                    name
                    if name in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
                    else "OTHER"
                )
                record(name, route_name(handler.path), elapsed, used, timing.lock_wait)

    return request


@contextmanager
def waiting() -> Iterator[None]:
    timing = CURRENT.get()
    wall = time.perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            timing.lock_wait += time.perf_counter() - wall


@contextmanager
def locked(lock: LockType) -> Iterator[None]:
    with waiting():
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def begin(connection: sqlite3.Connection, statement: str) -> None:
    with waiting():
        connection.execute(statement)


def snapshot(start: float, end: float) -> tuple[list, dict]:
    with GUARD:
        rows = [row for row in SAMPLES if start <= row[0] <= end]
        coverage = {
            "complete": STARTED_AT <= start and EVICTED_THROUGH < start,
            "started_at": STARTED_AT,
            "evicted_through": EVICTED_THROUGH if math.isfinite(EVICTED_THROUGH) else None,
            "capacity": SAMPLES.maxlen,
        }
        return rows, coverage


def report(hours: float) -> dict:
    end = time.time()
    start = end - hours * 3600
    rows, coverage = snapshot(start, end)
    writes = [row for row in rows if row[1] in {"POST", "PUT", "PATCH", "DELETE"}]
    walls = sorted(row[3] for row in writes)
    days, routes = {}, {}
    for ended, method, route, wall, cpu, lock_wait in rows:
        entry = routes.setdefault((method, route), {"method": method, "route": route, "count": 0, "cpu_seconds": 0.0})
        entry["count"] += 1
        entry["cpu_seconds"] += cpu
        if method in {"POST", "PUT", "PATCH", "DELETE"} and wall >= SLOW_SECONDS:
            day = datetime.fromtimestamp(ended, UTC).date().isoformat()
            days[day] = days.get(day, 0) + 1
    return {
        "window_start": start,
        "window_end": end,
        "coverage": coverage,
        "writes": {
            "count": len(writes),
            "p95_wall_seconds": walls[math.ceil(len(walls) * 0.95) - 1] if walls else None,
            "cpu_seconds": sum(row[4] for row in writes),
            "lock_wait_seconds": sum(row[5] for row in writes),
            "other_wall_seconds": sum(max(0.0, row[3] - row[4] - row[5]) for row in writes),
        },
        "slow_days": [{"day": day, "count": count} for day, count in sorted(days.items())],
        "routes": sorted(routes.values(), key=lambda entry: (-entry["cpu_seconds"], entry["method"], entry["route"])),
    }


def endpoint(method: Callable) -> Callable:
    @wraps(method)
    def request(handler):
        if handler.path.split("?", 1)[0] != "/api/v1/metrics":
            return method(handler)
        if handler.refused():
            return None
        try:
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
            hours = float(query.get("hours", ["24"])[0])
            if not math.isfinite(hours) or not 0 < hours <= RETENTION_SECONDS / 3600:
                raise ValueError
        except ValueError:
            return handler.send(
                400,
                json.dumps({"error": {"code": "invalid_window", "message": "hours out of range"}}),
                "application/json",
            )
        return handler.send(200, json.dumps(report(hours)), "application/json")

    return request


def main(argv: list[str] | None = None) -> None:
    from scripts.swarm_ledger import ledger_link

    host, port = ledger_link.address()
    parser = argparse.ArgumentParser(description="Ledger write timing and request route CPU")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--url", default=f"http://{host}:{port}")
    args = parser.parse_args(argv)
    if not math.isfinite(args.hours) or not 0 < args.hours <= RETENTION_SECONDS / 3600:
        parser.error("hours must be positive and at most seven days")
    with urlopen(f"{args.url.rstrip('/')}/api/v1/metrics?hours={args.hours}", timeout=10) as response:
        print(json.dumps(json.load(response), sort_keys=True))


if __name__ == "__main__":
    main()
