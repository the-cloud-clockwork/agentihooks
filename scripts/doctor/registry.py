"""A swarm's expected active bindings, joined with their exporter progress on this host and their accepted Langfuse observations."""

from __future__ import annotations

import json
import math
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from threading import Event

ACTIVE = "working"
TRACES_LIMIT = 5
ATTEMPTS = 2
READERS = 6
WINDOW_BYTES = 1 << 20
CORRELATION = "agentihooks.correlation."
wall = time.time
REMOTE_FIELDS = {
    "life": "agent.life",
    "seat": "seat",
    "task": "task",
    "harness": "harness",
    "profile": "profile.resolved",
}


def failure(exc: BaseException) -> str:
    frame = traceback.extract_tb(exc.__traceback__)[-1]
    notes = "".join(f"; {note}" for note in getattr(exc, "__notes__", ()))
    return f"{type(exc).__name__}: {exc} at {frame.filename}:{frame.lineno}{notes}"


def read_at() -> str:
    return _iso(int(wall() * 1000))


def failed_read(target: str, exc: BaseException) -> str:
    return f"{target} failed at {read_at()}: {failure(exc)}"


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ms(stamp: object) -> int:
    try:
        return int(datetime.fromisoformat(str(stamp)).timestamp() * 1000)
    except ValueError:
        return 0


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load(path: Path) -> dict:
    return _json(path)


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def _started_at(agent):
    return next((agent[key] for key in ("started_at", "launched_at") if (agent.get(key) or 0) > 0), 0)


def bindings(agents: list[dict]) -> list[dict]:
    return [
        {
            "agent": agent["name"],
            "life": f"{agent['name']}#{agent.get('started_at') or 0}",
            "seat": agent.get("seat") or "",
            "task": agent.get("task") or "",
            "harness": agent.get("harness") or "",
            "profile": agent.get("profile") or "",
            "started_at": _started_at(agent),
            "session_id": agent.get("conversation_id") or "",
        }
        for agent in agents
        if agent.get("state") == ACTIVE
    ]


def _oldest(transcript: str, offset: int, fallback_ms: int) -> int:
    try:
        with open(transcript, "rb") as handle:
            handle.seek(offset)
            window = handle.read(WINDOW_BYTES)
    except OSError:
        return fallback_ms
    for line in window.splitlines():
        try:
            stamp = json.loads(line).get("timestamp")
        except (ValueError, AttributeError):
            continue
        if stamp and _ms(stamp):
            return _ms(stamp)
    return fallback_ms


def _fallback(stat, cursor_path: Path, cursor: dict) -> int:
    """When no timestamped record is readable: the transcript time, else the oldest queued event, else the cursor time."""
    if stat:
        return int(stat.st_mtime * 1000)
    queued = [
        int(spec.get("end_ns", spec["start_ns"]) if spec.get("parent_id") is None else spec["start_ns"]) // 1_000_000
        for spec in cursor.get("pending") or []
        if spec.get("start_ns")
    ]
    return min(queued) if queued else int(cursor_path.stat().st_mtime * 1000)


def progress(session_id: str, harness: str) -> dict | None:
    """What the session generated against what its exporter had accepted, read from the exporter's own files."""
    from hooks.observability import agent_trace, trace_flush

    path = agent_trace._cursor_path(session_id)
    cursor = _json(path)
    request = _json(trace_flush.request_path(session_id))
    if not cursor and not request:
        return None
    transcript = trace_flush._transcript(session_id, {"target": harness, **request})
    source = cursor.get("source") or {}
    try:
        stat = Path(transcript).stat() if transcript else None
    except OSError:
        stat = None
    generated = max(stat.st_size if stat else 0, int(source.get("buffered_bytes", 0)))
    accepted_bytes = int(source.get("accepted_bytes", 0))
    unaccepted = generated > accepted_bytes
    owner = trace_flush._owner(_json(trace_flush.owner_path(session_id)), "supervisor_")
    return {
        "generated_bytes": generated,
        "accepted_bytes": accepted_bytes,
        "oldest_unaccepted": _oldest(transcript, accepted_bytes, _fallback(stat, path, cursor)) if unaccepted else 0,
        "pending": len(cursor.get("pending") or []),
        "overflow": int((cursor.get("overflow") or {}).get("bytes", 0)),
        "accepted": sum(1 for revision in (cursor.get("accepted") or {}).values() if revision != "legacy")
        + int((cursor.get("paged") or {}).get("spans", 0)),
        "accepted_at": _ms(cursor["accepted_at"]) if cursor.get("accepted_at") else 0,
        "exporter_alive": trace_flush.alive(owner),
        "requested_at": int(request["at"]) // 1_000_000 if request.get("at") else 0,
        "stopped": bool(request.get("stopped"))
        or request.get("reason") in trace_flush.STOP_REASONS
        or int(cursor.get("buffered_turns", 0)) > 1,
    }


def _root(get, trace_id: str) -> dict:
    rows = get("observations", {"traceId": trace_id, "type": "AGENT", "limit": 1, "page": 1})["data"]
    return rows[0] if rows else {}


def _remote_trace(row: dict, root: dict) -> dict:
    attributes = (root.get("metadata") or {}).get("attributes") or {}
    return {
        "id": row["id"],
        "session_id": row.get("sessionId") or "",
        **{name: str(attributes.get(CORRELATION + key) or "") for name, key in REMOTE_FIELDS.items()},
    }


def _freshness(get, trace_id: str, mark: dict, page: int) -> dict:
    params = {"traceId": trace_id, "limit": page, "page": 1}
    if mark.get("start_ms"):
        params["fromStartTime"] = _iso(mark["start_ms"])
    rows = get("observations", params)["data"]
    starts = [_ms(row.get("startTime")) for row in rows]
    ends = [_ms(row.get("endTime")) for row in rows]
    return {
        "start_ms": max([mark.get("start_ms", 0), *starts]),
        "fresh_ms": max([mark.get("fresh_ms", 0), *starts, *ends]),
    }


def _cached_listing(prior: dict, mark: dict, total: int, listing: Callable[[], set]) -> set:
    known = prior.get("listed")
    unchanged = known and known["total"] == total and prior["fresh_ms"] == mark["fresh_ms"]
    ids = set(known["ids"]) if unchanged else listing()
    mark["listed"] = {"total": total, "ids": sorted(ids)}
    return ids


def _read_binding(get, slug: str, binding: dict, marks: dict, kept: dict, page: int, ended: Event) -> dict:
    params = {
        "tags": [f"swarm:{slug}", f"agent:{binding['agent']}"],
        "fields": "core",
        "orderBy": "timestamp.desc",
        "limit": TRACES_LIMIT,
        "page": 1,
    }
    found = [_remote_trace(row, _root(get, row["id"])) for row in get("traces", params)["data"]]
    own = binding["session_id"]
    current = next((t for t in found if t["session_id"] == own), None) if own else (found or [None])[0]
    if current is None:
        return {"read": True, "traces": found, "remote": None}
    prior = marks.get(current["id"], {})
    mark = _freshness(get, current["id"], prior, page)
    kept[current["id"]] = mark
    total = get("observations", {"traceId": current["id"], "limit": 1, "page": 1})["meta"].get("totalItems") or 0
    from scripts.doctor.observation_reconciliation import reconcile

    listed = partial(_cached_listing, prior, mark, int(total))
    reconciled = reconcile(get, own or current["session_id"], binding["harness"], current["id"], ended.is_set, listed)
    remote = {"trace": current["id"], "fresh_ms": mark["fresh_ms"], "observations": int(total)}
    if reconciled is not None:
        remote["reconciliation"] = reconciled
    return {
        "read": True,
        "traces": found,
        "session_id": own or current["session_id"],
        "remote": remote,
    }


def _unread() -> dict:
    return {"read": False, "traces": [], "remote": None}


def _attempts(get, slug: str, binding: dict, marks: dict, page: int, ended: Event, slot: dict) -> tuple[dict, dict]:
    tries = 0
    while tries < ATTEMPTS and not ended.is_set():
        tries += 1
        kept: dict = {}
        try:
            remote = _read_binding(get, slug, binding, marks, kept, page, ended)
            slot.clear()
            return remote, kept
        except Exception as exc:  # noqa: BLE001
            slot["error"] = failed_read(f"active read of {binding['agent']}", exc)
    return _unread(), {}


def _read_all(get, slug: str, active: list[dict], marks: dict, page: int, remaining: float) -> list[tuple]:
    if remaining <= 0:
        return [(_unread(), {}, None) for _ in active]
    slots: list[dict] = [{} for _ in active]
    ended, pool = Event(), ThreadPoolExecutor(max_workers=READERS)
    futures = [pool.submit(_attempts, get, slug, b, marks, page, ended, slot) for b, slot in zip(active, slots)]
    wait(futures, timeout=remaining)
    ended.set()
    pool.shutdown(wait=False)
    done = [f.result() if f.done() else (_unread(), {}) for f in futures]
    return [(remote, kept, slot.get("error")) for (remote, kept), slot in zip(done, slots)]


def _size(binding: dict, sizes: dict) -> float:
    if binding["agent"] in sizes:
        return sizes[binding["agent"]]
    return math.inf if binding["agent"].startswith("master@") else 0


def _largest_first(get, slug: str, active: list[dict], marks: dict, page: int, remaining: float, sizes: dict) -> list:
    order = sorted(range(len(active)), key=lambda i: -_size(active[i], sizes))
    ranked = dict(zip(order, _read_all(get, slug, [active[i] for i in order], marks, page, remaining)))
    return [ranked[i] for i in range(len(active))]


def read(
    slug: str,
    agents: list[dict],
    get: Callable[[str, dict], dict],
    state: dict,
    seconds: float,
    page: int,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict], list[str]]:
    """Every active binding with its local progress and, inside the read budget, its Langfuse traces and freshness."""
    deadline = clock() + seconds
    marks, kept, failures, found = state.get("marks", {}), {}, [], []
    active, sizes = bindings(agents), state.get("sizes", {})
    results = _largest_first(get, slug, active, marks, page, deadline - clock(), sizes)
    for binding, (remote, marked, error) in zip(active, results):
        kept.update(marked)
        if not remote["read"]:
            error = error or f"active read budget of {seconds:g} seconds spent before {binding['agent']} at {read_at()}"
        merged = {**binding, **remote, "local": None}
        try:
            merged["local"] = progress(merged["session_id"], merged["harness"]) if merged["session_id"] else None
        except (OSError, ValueError, TypeError) as exc:
            error = error or failed_read(f"exporter progress read of {binding['agent']}", exc)
        failures += [error] if error else []
        found.append(merged)
    state["marks"] = {**marks, **kept} if failures else kept
    state["sizes"] = {
        **{b["agent"]: sizes[b["agent"]] for b in active if b["agent"] in sizes},
        **{b["agent"]: b["remote"]["observations"] for b in found if b["remote"]},
    }
    return found, failures
