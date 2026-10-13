import argparse
import json
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from urllib.request import urlopen

MAX_SAMPLES = 50000
RETENTION_SECONDS = 7 * 86400
SLOW_SECONDS = 5.0
CURRENT = ContextVar("ledger_request_timing", default=None)
SAMPLES = deque(maxlen=MAX_SAMPLES)
GUARD = threading.Lock()


@dataclass
class Timing:
    lock_wait: float = 0.0


def route_name(path: str) -> str:
    parts = path.split("?", 1)[0].strip("/").split("/")
    if parts[:3] == ["api", "v1", "ledgers"] and len(parts) > 3:
        resource = parts[4] if len(parts) > 4 else "metadata"
        return f"/api/v1/ledgers/:slug/{resource}"
    return "/" + "/".join(parts)


def record(method: str, route: str, wall: float, cpu: float, lock_wait: float) -> None:
    ended = time.time()
    with GUARD:
        SAMPLES.append((ended, method, route, wall, cpu, lock_wait))
        while SAMPLES and SAMPLES[0][0] < ended - RETENTION_SECONDS:
            SAMPLES.popleft()


def measured(method):
    @wraps(method)
    def request(handler):
        timing = Timing()
        token = CURRENT.set(timing)
        wall, cpu = time.perf_counter(), time.thread_time()
        try:
            return method(handler)
        finally:
            elapsed, used = time.perf_counter() - wall, time.thread_time() - cpu
            CURRENT.reset(token)
            record(method.__name__.removeprefix("do_"), route_name(handler.path), elapsed, used, timing.lock_wait)

    return request


@contextmanager
def waiting():
    timing = CURRENT.get()
    wall = time.perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            timing.lock_wait += time.perf_counter() - wall


@contextmanager
def locked(lock):
    with waiting():
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def begin(connection, statement: str) -> None:
    with waiting():
        connection.execute(statement)


def report(hours: float) -> dict:
    end = time.time()
    start = end - hours * 3600
    with GUARD:
        rows = [row for row in SAMPLES if start <= row[0] <= end]
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
