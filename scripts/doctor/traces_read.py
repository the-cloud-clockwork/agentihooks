"""Read a swarm's Langfuse traces, its active bindings and the sessions that should have traces into the detector record.

Active bindings and the historical backfill each read inside their own time budget and page caps; each trace's
observations are cached until the trace changes. Failures and partial coverage come back in the record's reader
state, never as an empty list.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from hooks.observability.signals import safe_name
from scripts.doctor import registry
from scripts.swarm.runtime import SWARM_HOME

DEFAULT_HOST = "https://langfuse.homeofanton.com"
PAGE = 100
OBSERVATION_PAGE = 50
WORKED = {"claimed", "pr", "done"}
STATE_FILE = "doctor-telemetry.json"
CACHE_DIR = "doctor-traces"


@dataclass(frozen=True)
class Budget:
    active_seconds: float = 15.0
    history_seconds: float = 45.0
    request_seconds: float = 10.0
    trace_pages: int = 20
    observation_pages: int = 20
    active_page: int = 10


def client(environ: Mapping[str, str], timeout: float = Budget.request_seconds) -> Callable[[str, dict], dict]:
    host = (environ.get("LANGFUSE_HOST") or DEFAULT_HOST).rstrip("/")
    auth = (environ.get("LANGFUSE_PUBLIC_KEY", ""), environ.get("LANGFUSE_SECRET_KEY", ""))

    def get(path: str, params: dict) -> dict:
        if not all(auth):
            raise RuntimeError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set")
        response = httpx.get(f"{host}/api/public/{path}", params=params, auth=auth, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return get


def _pages(get, path, params, cap):
    rows, page = [], 1
    while True:
        body = get(path, {**params, "page": page, "limit": OBSERVATION_PAGE})
        rows += body["data"]
        if page >= body["meta"]["totalPages"]:
            return rows, True
        if page >= cap:
            return rows, False
        page += 1


def _ms(stamp):
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000) if stamp else 0


def _observation(row):
    attributes = (row.get("metadata") or {}).get("attributes") or {}
    turn = attributes.get("agent.turn")
    error = attributes.get("error")
    usage = row.get("usageDetails") or {}
    return {
        "type": row["type"],
        "name": row.get("name") or "",
        "start": _ms(row.get("startTime")),
        "end": _ms(row.get("endTime") or row.get("startTime")),
        "turn": int(turn) if turn is not None else None,
        "error": str(error).lower() == "true" if error is not None else None,
        "tokens": int(row.get("totalTokens") or 0) - int(usage.get("cache_read_input_tokens") or 0),
    }


def _cache_path(cache: Path, trace_id: str) -> Path:
    return cache / f"{safe_name(trace_id)}.json"


def _cached(cache: Path, trace: dict) -> dict:
    entry = registry.load(_cache_path(cache, trace["id"]))
    usable = "observations" in entry or "failed" in entry
    return entry if usable and entry.get("updated_at") == trace.get("updatedAt") else {}


def _listing(slug, get, budget, deadline, clock):
    rows, page = [], 1
    try:
        while True:
            body = get("traces", {"tags": f"swarm:{slug}", "fields": "core", "page": page, "limit": PAGE})
            rows += body["data"]
            if page >= body["meta"]["totalPages"]:
                return rows, True, []
            if page >= budget.trace_pages or clock() >= deadline:
                return rows, False, []
            page += 1
    except Exception as exc:  # noqa: BLE001
        return rows, False, [f"trace listing failed: {type(exc).__name__}: {exc}"]


def _backfill(get, cache, trace, budget):
    try:
        rows, whole = _pages(get, "observations", {"traceId": trace["id"]}, budget.observation_pages)
        entry = {"updated_at": trace.get("updatedAt"), "whole": whole, "observations": [_observation(o) for o in rows]}
    except Exception as exc:  # noqa: BLE001
        entry = {"updated_at": trace.get("updatedAt"), "failed": f"{type(exc).__name__}: {exc}"}
    registry.save(_cache_path(cache, trace["id"]), entry)
    return entry


def history(
    slug: str,
    get: Callable[[str, dict], dict],
    cache: Path,
    budget: Budget,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict], dict, list[str]]:
    """Every listed trace with its observations: unchanged ones from the cache, the rest newest first in budget.

    A trace whose own read fails is a coverage gap retried after every unread trace; only a failed listing is a
    reader failure.
    """
    deadline = clock() + budget.history_seconds
    listed, complete_listing, failures = _listing(slug, get, budget, deadline, clock)
    entries = {t["id"]: _cached(cache, t) for t in listed}
    unread = [t for t in listed if not entries[t["id"]]]
    retried = [t for t in listed if entries[t["id"]].get("failed")]
    for trace in [] if failures else unread + retried:
        if clock() >= deadline:
            break
        entries[trace["id"]] = _backfill(get, cache, trace, budget)
    found = [
        {
            "id": trace["id"],
            "name": trace.get("name") or "",
            "session_id": trace.get("sessionId") or "",
            "tags": sorted(trace.get("tags") or []),
            "observations": entries[trace["id"]].get("observations", []),
        }
        for trace in listed
    ]
    covered = sum(1 for entry in entries.values() if entry.get("whole"))
    coverage = {
        "listed": complete_listing,
        "traces": len(listed),
        "covered": covered,
        "complete": complete_listing and covered == len(listed),
        "unreadable": [f"trace {key} unreadable: {e['failed']}" for key, e in entries.items() if e.get("failed")],
    }
    return found, coverage, failures


def sessions(tasks: list[dict], agents: list[dict]) -> list[dict]:
    found = {
        t["claimed_by"]: {"agent": t["claimed_by"], "session_id": "", "task": t["id"], "started_at": 0}
        for t in tasks
        if t.get("claimed_by") and t.get("state") in WORKED
    }
    for agent in agents:
        found[agent["name"]] = {
            "agent": agent["name"],
            "session_id": agent.get("conversation_id") or "",
            "task": agent.get("task") or "",
            "started_at": agent.get("started_at") or 0,
        }
    return sorted(found.values(), key=lambda s: s["agent"])


def merged(tasks: list[dict]) -> list[str]:
    return sorted(t["id"] for t in tasks if t.get("state") == "done" and t.get("pr_url"))


def _empty_project_failure(slug, get, active, traces, coverage) -> str:
    accepted = [b["agent"] for b in active if (b["local"] or {}).get("accepted")]
    if traces or not coverage["listed"] or not accepted or any(b["traces"] for b in active):
        return ""
    try:
        project = get("projects", {})["data"][0]["name"]
    except Exception as exc:  # noqa: BLE001
        project = f"unknown ({type(exc).__name__})"
    return (
        f"Langfuse project {project} holds no trace tagged swarm:{slug} while the exporters of "
        f"{', '.join(accepted)} report accepted exports: the reader's keys may belong to another project"
    )


def record(
    slug,
    tasks,
    agents,
    now_ms,
    get,
    home=None,
    budget: Budget = Budget(),
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    base = Path(home or SWARM_HOME) / slug
    state = registry.load(base / STATE_FILE)
    active, failures = registry.read(slug, agents, get, state, budget.active_seconds, budget.active_page, clock)
    traces, coverage, lost = history(slug, get, base / CACHE_DIR, budget, clock)
    failures += lost
    empty = _empty_project_failure(slug, get, active, traces, coverage)
    if empty:
        failures.append(empty)
        active = [{**binding, "read": False} for binding in active]
        coverage = {**coverage, "listed": False, "complete": False}
    state["down_since"] = (state.get("down_since") or now_ms) if failures else 0
    registry.save(base / STATE_FILE, state)
    return {
        "slug": slug,
        "now_ms": now_ms,
        "traces": traces,
        "sessions": sessions(tasks, agents),
        "merged": merged(tasks),
        "active": active,
        "reader": {
            "failures": failures,
            "down_since": state["down_since"],
            "active": {"bindings": len(active), "read": sum(1 for b in active if b["read"])},
            "historical": coverage,
        },
    }


def main(argv=None) -> int:
    from scripts.doctor import traces as detector
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import connect

    slug = (argv if argv is not None else sys.argv[1:])[0]
    agents = [asdict(a) for a in connect().agents(slug)]
    now_ms = int(datetime.now().timestamp() * 1000)
    data = record(slug, LedgerClient().tasks(slug), agents, now_ms, client(os.environ))
    limits = detector.Limits.from_env(os.environ)
    found = [asdict(f) | {"id": f.id} for f in detector.findings(data, limits)]
    print(json.dumps({"measures": detector.measures(data), "findings": found}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
