"""Read a swarm's Langfuse traces and the sessions that should have them into the record the trace detector takes."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime

import httpx

DEFAULT_HOST = "https://langfuse.homeofanton.com"
PAGE = 100
WORKED = {"claimed", "pr", "done"}


def client(environ: Mapping[str, str]) -> Callable[[str, dict], dict]:
    host = (environ.get("LANGFUSE_HOST") or DEFAULT_HOST).rstrip("/")
    auth = (environ.get("LANGFUSE_PUBLIC_KEY", ""), environ.get("LANGFUSE_SECRET_KEY", ""))
    if not all(auth):
        raise RuntimeError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set")

    def get(path: str, params: dict) -> dict:
        response = httpx.get(f"{host}/api/public/{path}", params=params, auth=auth, timeout=30)
        response.raise_for_status()
        return response.json()

    return get


def _all(get, path, params):
    rows, page = [], 1
    while True:
        body = get(path, {**params, "page": page, "limit": PAGE})
        rows += body["data"]
        if page >= body["meta"]["totalPages"]:
            return rows
        page += 1


def _ms(stamp):
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000) if stamp else 0


def _observation(row):
    attributes = (row.get("metadata") or {}).get("attributes") or {}
    turn = attributes.get("agent.turn")
    error = attributes.get("error")
    return {
        "type": row["type"],
        "name": row.get("name") or "",
        "start": _ms(row.get("startTime")),
        "end": _ms(row.get("endTime") or row.get("startTime")),
        "turn": int(turn) if turn is not None else None,
        "error": str(error).lower() == "true" if error is not None else None,
        "tokens": int(row.get("totalTokens") or 0),
    }


def traces(slug: str, get: Callable[[str, dict], dict]) -> list[dict]:
    return [
        {
            "id": trace["id"],
            "name": trace.get("name") or "",
            "session_id": trace.get("sessionId") or "",
            "tags": sorted(trace.get("tags") or []),
            "observations": [_observation(o) for o in _all(get, "observations", {"traceId": trace["id"]})],
        }
        for trace in _all(get, "traces", {"tags": f"swarm:{slug}"})
    ]


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


def record(slug, tasks, agents, now_ms, get) -> dict:
    return {
        "slug": slug,
        "now_ms": now_ms,
        "traces": traces(slug, get),
        "sessions": sessions(tasks, agents),
        "merged": merged(tasks),
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
