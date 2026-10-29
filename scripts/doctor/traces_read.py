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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from hooks.config import langfuse_route
from hooks.observability.signals import safe_name
from scripts import profile_telemetry
from scripts.doctor import registry
from scripts.swarm.runtime import SWARM_HOME

DEFAULT_HOST = "https://langfuse.homeofanton.com"
SWARM_PROJECT = "agent-swarm"
CALLER_ROUTE = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
INGRESS = httpx.URL(langfuse_route({})[0]).host
PAGE = 100
OBSERVATION_PAGE = 50
WORKED = {"claimed", "pr", "done"}
STATE_FILE = "doctor-telemetry.json"
CACHE_DIR = "doctor-traces"
elapsed = time.perf_counter


@dataclass(frozen=True)
class Budget:
    active_seconds: float = 15.0
    history_seconds: float = 45.0
    wait_seconds: float = 15.0
    step_seconds: float = 55.0
    request_seconds: float = 7.0
    trace_pages: int = 20
    observation_pages: int = 20
    active_page: int = 10


def client(environ: Mapping[str, str], timeout: float = Budget.request_seconds) -> Callable[[str, dict], dict]:
    host = (environ.get("LANGFUSE_HOST") or DEFAULT_HOST).rstrip("/")
    auth = (environ.get("LANGFUSE_PUBLIC_KEY", ""), environ.get("LANGFUSE_SECRET_KEY", ""))
    send = _through_ingress if host == DEFAULT_HOST else _direct

    def get(path: str, params: dict) -> dict:
        if not all(auth):
            raise RuntimeError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set")
        timings = _Timings()
        try:
            response = send(
                f"{host}/api/public/{path}", params=params, auth=auth, timeout=timeout, extensions={"trace": timings}
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            exc.add_note(timings.summary(path))
            raise

    return get


class _Timings:
    """An httpx trace hook keeping only each transport step's seconds since the request started; the values
    httpcore passes it (headers, keys, bodies) are never kept."""

    MARKS = (("connect", "connect_tcp"), ("TLS", "start_tls"), ("first byte", "receive_response_headers"))

    def __init__(self):
        self.started_at, self.start = registry.read_at(), elapsed()
        self.marks: dict[str, float] = {}
        self.phase = "before connect"

    def __call__(self, name: str, info: dict) -> None:
        step, edge = name.split(".")[-2:]
        if edge == "started":
            self.phase = f"in {step}"
        elif edge == "complete":
            self.marks[step] = elapsed() - self.start
            self.phase = f"after {step}"

    def summary(self, path: str) -> str:
        reached = ", ".join(
            f"{label} {self.marks[step]:.3f} s" if step in self.marks else f"{label} not reached"
            for label, step in self.MARKS
        )
        return (
            f"request {path} started at {self.started_at}, {reached}, "
            f"total {elapsed() - self.start:.3f} s, failed {self.phase}"
        )


def _direct(url: str, **options) -> httpx.Response:
    with httpx.Client() as session:
        return session.get(url, **options)


def _through_ingress(url: str, extensions: dict, **options) -> httpx.Response:
    public = httpx.URL(url)
    with httpx.Client() as session:
        return session.get(
            public.copy_with(host=INGRESS),
            headers={"Host": public.host},
            extensions={**extensions, "sni_hostname": public.host},
            **options,
        )


def installed_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The caller's environment with its Langfuse host and keys replaced by the installed swarm telemetry keys,
    so the installed secret only ever reaches the default host."""
    keys = {name.removeprefix("OTEL_"): value for name, value in profile_telemetry.agentihooks_langfuse_keys().items()}
    return {name: value for name, value in environ.items() if name not in CALLER_ROUTE} | keys


def measured_client(environ: Mapping[str, str]) -> Callable[[str, dict], dict]:
    """The reader on the installed keys, refused unless they serve the watched swarm's Langfuse project."""
    get = client(installed_env(environ))
    try:
        names = [project.get("name") for project in get("projects", {})["data"]]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Langfuse project identity is unavailable: {type(exc).__name__}: {exc}") from exc
    if not names or not all(names):
        raise RuntimeError("Langfuse project identity is unavailable: the installed keys name no project")
    if names != [SWARM_PROJECT]:
        raise RuntimeError(
            f"the installed Langfuse keys serve project {', '.join(names)}, "
            f"not the watched swarm project {SWARM_PROJECT}"
        )
    return get


def _pages(get, path, params, cap, deadline, clock):
    rows, page = {}, 1
    while True:
        body = get(path, {**params, "page": page, "limit": OBSERVATION_PAGE})
        rows.update({row.get("id", (page, index)): row for index, row in enumerate(body["data"])})
        if page >= body["meta"]["totalPages"]:
            return list(rows.values()), True, False
        if page >= cap or clock() >= deadline:
            return list(rows.values()), False, page >= cap
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
    path = _cache_path(cache, trace["id"])
    entry = registry.load(path)
    if entry.get("updated_at") != trace.get("updatedAt") or not _settled(entry):
        return {}
    return entry if entry.get("deduplicated") else _upgraded(path, entry)


def _upgraded(path: Path, entry: dict) -> dict:
    observations = [json.dumps(o) for o in entry.get("observations", ())]
    if len(set(observations)) < len(observations):
        return {}
    entry = {**entry, "deduplicated": True}
    registry.save(path, entry)
    return entry


def _listing(slug, get, budget, deadline, clock):
    rows, page = [], 1
    try:
        while True:
            body = get("traces", {"tags": f"swarm:{slug}", "fields": "core", "page": page, "limit": PAGE})
            rows += body["data"]
            if page >= body["meta"]["totalPages"]:
                return rows, True, 0, []
            if page >= budget.trace_pages or clock() >= deadline:
                return rows, False, max(body["meta"]["totalItems"] - len({row["id"] for row in rows}), 0), []
            page += 1
    except Exception as exc:  # noqa: BLE001
        return rows, False, 0, [registry.failed_read("trace listing", exc)]


def _backfill(get, cache, trace, budget, deadline, clock):
    try:
        rows, whole, capped = _pages(
            get, "observations", {"traceId": trace["id"]}, budget.observation_pages, deadline, clock
        )
        entry = {"updated_at": trace.get("updatedAt"), "whole": whole, "observations": [_observation(o) for o in rows]}
        if capped:
            entry["capped"] = True
    except Exception as exc:  # noqa: BLE001
        entry = {"updated_at": trace.get("updatedAt"), "failed": registry.failed_read("observation read", exc)}
    entry["deduplicated"] = True
    if _settled(entry):
        registry.save(_cache_path(cache, trace["id"]), entry)
    return entry


def _settled(entry: dict) -> bool:
    return bool(entry.get("whole") or entry.get("capped") or "failed" in entry)


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
    listed, complete_listing, unlisted, failures = _listing(slug, get, budget, deadline, clock)
    listed = list({trace["id"]: trace for trace in listed}.values())
    entries = {t["id"]: _cached(cache, t) for t in listed}
    unread = [t for t in listed if not entries[t["id"]]]
    retried = [t for t in listed if entries[t["id"]].get("failed")]
    for trace in [] if failures else unread + retried:
        if clock() >= deadline:
            break
        entries[trace["id"]] = _backfill(get, cache, trace, budget, deadline, clock)
    found = [
        {
            "id": trace["id"],
            "name": trace.get("name") or "",
            "session_id": trace.get("sessionId") or "",
            "tags": sorted(trace.get("tags") or []),
            "observations": entries[trace["id"]].get("observations", []),
            "complete": bool(entries[trace["id"]].get("whole")),
        }
        for trace in listed
    ]
    covered = sum(1 for entry in entries.values() if entry.get("whole"))
    capped = sum(1 for entry in entries.values() if entry.get("capped"))
    coverage = {
        "listed": complete_listing,
        "unlisted": unlisted,
        "traces": len(listed),
        "covered": covered,
        "capped": capped,
        "complete": not failures and covered + capped == len(listed),
        "unreadable": [f"trace {key} unreadable: {e['failed']}" for key, e in entries.items() if e.get("failed")],
    }
    return found, coverage, failures


def waits(
    read: Callable[[str], list[dict]],
    names: list[str],
    started: float,
    budget: Budget,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, list[dict]], list[str]]:
    """Each named agent's wait history until the wait budget or the step that started at `started` is spent;
    the agents left unread are returned as a list."""
    deadline = min(started + budget.step_seconds, clock() + budget.wait_seconds)
    found = {}
    for name in names:
        if clock() >= deadline:
            break
        found[name] = read(name)
    return found, names[len(found) :]


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
    with ThreadPoolExecutor() as pool:
        reading = pool.submit(registry.read, slug, agents, get, state, budget.active_seconds, budget.active_page, clock)
        traces, coverage, lost = history(slug, get, base / CACHE_DIR, budget, clock)
        active, failures = reading.result()
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
