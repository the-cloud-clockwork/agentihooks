import argparse
import json
import math
import os
import sqlite3
import tempfile
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
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

MAX_SAMPLES = 50000
RETENTION_SECONDS = 7 * 86400
SLOW_SECONDS = 5.0
DEFAULT_RECORDS = 1000
MAX_RECORDS = 10000
JOURNAL_NAME = ".request-metrics.jsonl"
WRITES = frozenset(("POST", "PUT", "PATCH", "DELETE"))
JOURNAL_WIDTHS = {"held": 2, "lost": 3, "evicted": 2, "start": 2, "flush": 2, "stop": 2, "s": 7}
CURRENT = ContextVar("ledger_request_timing", default=None)
SAMPLES = deque(maxlen=MAX_SAMPLES)
PENDING = []
GUARD = threading.Lock()
JOURNAL_GUARD = threading.Lock()
STARTED_AT = time.time()
EVICTED_THROUGH = float("-inf")
HELD_SINCE = None
LOST = []
JOURNAL = None
APPENDED = 0
STALE = False


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


def hold(row: tuple) -> None:
    global EVICTED_THROUGH

    if len(SAMPLES) == SAMPLES.maxlen:
        EVICTED_THROUGH = max(EVICTED_THROUGH, SAMPLES[0][0])
    SAMPLES.append(row)


def record(method: str, route: str, wall: float, cpu: float, lock_wait: float) -> None:
    global EVICTED_THROUGH

    with GUARD:
        ended = time.time()
        row = (ended, method, route, wall, cpu, lock_wait)
        hold(row)
        if JOURNAL is not None:
            PENDING.append(row)
        while SAMPLES and SAMPLES[0][0] < ended - RETENTION_SECONDS:
            expired = SAMPLES.popleft()
            EVICTED_THROUGH = max(EVICTED_THROUGH, expired[0])


def journal_rows(path: Path) -> Iterator[list]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if journal_row(row):
            yield row


def journal_row(row: object) -> bool:
    if not isinstance(row, list) or not row or not isinstance(row[0], str):
        return False
    if len(row) != JOURNAL_WIDTHS.get(row[0]):
        return False
    texts = (2, 3) if row[0] == "s" else ()
    return all(
        isinstance(value, str) if index in texts else type(value) in (int, float) and math.isfinite(value)
        for index, value in enumerate(row[1:], 1)
    )


def replay(rows: Iterator[list], started_at: float) -> tuple[list, float | None, list, float]:
    samples, held, lost, open_since, evicted = [], None, [], None, -math.inf
    for kind, stamp, *rest in rows:
        if kind == "held":
            held = stamp
        elif kind == "lost":
            lost.append((stamp, rest[0]))
        elif kind == "evicted":
            evicted = max(evicted, stamp)
        elif kind == "start":
            if open_since is not None:
                lost.append((open_since, stamp))
            held = stamp if held is None else held
            open_since = stamp
        elif kind == "stop":
            open_since = None
        elif open_since is not None:
            open_since = max(open_since, stamp)
        if kind == "s":
            samples.append((stamp, *rest))
    if open_since is not None:
        lost.append((open_since, started_at))
    return samples, held, lost, evicted


def held_since() -> float:
    return HELD_SINCE if HELD_SINCE is not None else STARTED_AT


def journal_lines(flushed: float | None) -> list:
    lines = [["held", held_since()], *(["lost", *span] for span in LOST), *evicted_line(), ["start", STARTED_AT]]
    lines += [["s", *row] for row in SAMPLES]
    return lines + ([["flush", flushed]] if flushed is not None else [])


def evicted_line() -> list:
    return [["evicted", EVICTED_THROUGH]] if math.isfinite(EVICTED_THROUGH) else []


def encode(lines: list) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def append_lines(path: Path, lines: list) -> None:
    with path.open("a") as handle:
        handle.write(encode(lines))


def replace_lines(path: Path, lines: list) -> None:
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(encode(lines))
    Path(staged).replace(path)


def restore(path: Path) -> None:
    global JOURNAL, HELD_SINCE, APPENDED, EVICTED_THROUGH, STALE

    with JOURNAL_GUARD:
        samples, held, lost, evicted = replay(journal_rows(path), STARTED_AT)
        with GUARD:
            floor = time.time() - RETENTION_SECONDS
            current = list(SAMPLES)
            SAMPLES.clear()
            for row in [row for row in samples if row[0] >= floor] + current:
                hold(row)
            EVICTED_THROUGH = max(EVICTED_THROUGH, evicted)
            LOST[:] = [span for span in lost if span[1] >= floor]
            HELD_SINCE = max(held, floor) if held is not None else None
            PENDING.clear()
            JOURNAL, STALE = path, True
            lines = journal_lines(None)
        replace_lines(path, lines)
        with GUARD:
            APPENDED, STALE = 0, False


def stop() -> None:
    persist(stopped=True)


def persist(stopped: bool = False) -> None:
    global APPENDED, STALE

    with JOURNAL_GUARD:
        with GUARD:
            if JOURNAL is None or not (PENDING or stopped or STALE):
                return
            now = time.time()
            rewrite = STALE or APPENDED + len(PENDING) > SAMPLES.maxlen
            if rewrite:
                lines, APPENDED = journal_lines(now), 0
            else:
                lines = [["s", *row] for row in PENDING] + evicted_line() + [["flush", now]]
                APPENDED += len(lines)
            PENDING.clear()
            path, STALE = JOURNAL, False
        lines += [["stop", now]] if stopped else []
        try:
            (replace_lines if rewrite else append_lines)(path, lines)
        except OSError:
            with GUARD:
                STALE = True
            raise


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
        held = held_since()
        lost = [{"start": first, "end": last} for first, last in LOST if first <= end and last >= start]
        coverage = {
            "complete": held <= start and EVICTED_THROUGH < start and not lost,
            "started_at": STARTED_AT,
            "held_since": held,
            "lost": lost,
            "evicted_through": EVICTED_THROUGH if math.isfinite(EVICTED_THROUGH) else None,
            "capacity": SAMPLES.maxlen,
        }
        return rows, coverage


def records(hours: float, end: float | None, limit: int) -> dict:
    end = time.time() if end is None else end
    start = end - hours * 3600
    rows, coverage = snapshot(start, end)
    writes = [row for row in rows if row[1] in WRITES]
    kept = writes[-limit:]
    routes = {}
    for ended, method, route, wall, cpu, lock_wait in kept:
        entry = routes.setdefault((method, route), {"method": method, "route": route, "count": 0, "records": []})
        entry["count"] += 1
        entry["records"].append(
            {"ended": ended, "wall_seconds": wall, "cpu_seconds": cpu, "lock_wait_seconds": lock_wait}
        )
    return {
        "window_start": start,
        "window_end": end,
        "coverage": coverage,
        "matched": len(writes),
        "truncated": len(kept) < len(writes),
        "routes": list(routes.values()),
    }


def report(hours: float, end: float | None = None) -> dict:
    end = time.time() if end is None else end
    start = end - hours * 3600
    rows, coverage = snapshot(start, end)
    writes = [row for row in rows if row[1] in WRITES]
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


def reader(query: dict) -> Callable[[], dict]:
    hours = float(query.get("hours", ["24"])[0])
    end = float(query["end"][0]) if "end" in query else None
    if not math.isfinite(hours) or not 0 < hours <= RETENTION_SECONDS / 3600:
        raise ValueError
    if end is not None and not math.isfinite(end):
        raise ValueError
    view = query.get("view", ["summary"])[0]
    if view == "summary":
        return lambda: report(hours, end)
    limit = int(query.get("limit", [str(DEFAULT_RECORDS)])[0])
    if view != "writes" or not 0 < limit <= MAX_RECORDS:
        raise ValueError
    return lambda: records(hours, end, limit)


def endpoint(method: Callable) -> Callable:
    @wraps(method)
    def request(handler):
        if handler.path.split("?", 1)[0] != "/api/v1/metrics":
            return method(handler)
        if handler.refused():
            return None
        try:
            produce = reader(parse_qs(urlsplit(handler.path).query, keep_blank_values=True))
        except ValueError:
            return handler.send(
                400,
                json.dumps({"error": {"code": "invalid_window", "message": "hours out of range"}}),
                "application/json",
            )
        return handler.send(200, json.dumps(produce()), "application/json")

    return request


def urlopen(url: str, timeout: float):
    from scripts.swarm_ledger import ledger_link

    return ledger_link.urlopen(Request(url), timeout)


def main(argv: list[str] | None = None) -> None:
    from scripts.swarm_ledger import ledger_link

    parser = argparse.ArgumentParser(description="Ledger write timing and request route CPU")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--url")
    parser.add_argument("--end", type=float)
    parser.add_argument("--writes", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_RECORDS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.hours) or not 0 < args.hours <= RETENTION_SECONDS / 3600:
        parser.error("hours must be positive and at most seven days")
    if args.end is not None and not math.isfinite(args.end):
        parser.error("end must be a finite epoch time")
    if not 0 < args.limit <= MAX_RECORDS:
        parser.error(f"limit must be between 1 and {MAX_RECORDS}")
    query = f"hours={args.hours}"
    query += f"&end={args.end}" if args.end is not None else ""
    query += f"&view=writes&limit={args.limit}" if args.writes else ""
    url = args.url or ledger_link.base()
    with urlopen(f"{url.rstrip('/')}/api/v1/metrics?{query}", timeout=10) as response:
        print(json.dumps(json.load(response), sort_keys=True))


if __name__ == "__main__":
    main()
