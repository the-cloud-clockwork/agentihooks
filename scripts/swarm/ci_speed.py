"""The median minutes of Tests runs on pull requests into dev over the last day, cached in Redis and read hourly."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.swarm.store import PREFIX

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import RedisStore, SwarmConfig

WORKFLOW = "test.yml"
WINDOW_S = 24 * 3600
REFRESH_MS = 3600 * 1000
OVERLAP_S = 2 * 3600
FINISHED = {"success", "failure"}
JQ = ".workflow_runs[] | {id, event, status, conclusion, head_branch, run_started_at, updated_at} | @json"
EMPTY = {"minutes": None, "runs": 0, "at": 0, "tried_at": 0}


def key(slug: str) -> str:
    return ":".join((PREFIX, slug, "ci-speed"))


def get(redis: Redis, slug: str) -> dict | None:
    raw = redis.get(key(slug))
    return json.loads(raw) if raw else None


def _seconds(stamp):
    return datetime.fromisoformat(stamp).timestamp()


def finished(runs: list[dict]) -> list[dict]:
    """Head dev marks a pull request into main: run payloads mostly drop their pull request link and its base."""
    return [
        r
        for r in runs
        if r["event"] == "pull_request"
        and r["status"] == "completed"
        and r["conclusion"] in FINISHED
        and r["head_branch"] != "dev"
    ]


def durations(runs: list[dict]) -> dict[str, list[float]]:
    return {
        str(r["id"]): [_seconds(r["run_started_at"]), _seconds(r["updated_at"]) - _seconds(r["run_started_at"])]
        for r in finished(runs)
    }


def median_minutes(window: dict[str, list[float]]) -> float | None:
    seconds = [spent for _, spent in window.values()]
    return round(statistics.median(seconds) / 60, 2) if seconds else None


def read_runs(repo_dir: str, since_s: float, run: Callable = subprocess.run) -> list[dict]:
    since = datetime.fromtimestamp(since_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def read(conclusion):
        endpoint = (
            f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs"
            f"?event=pull_request&status={conclusion}&exclude_pull_requests=true&created=>={since}&per_page=100"
        )
        return run(
            ["gh", "api", endpoint, "--paginate", "--jq", JQ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout

    with ThreadPoolExecutor() as pool:
        outputs = list(pool.map(read, sorted(FINISHED)))
    return [json.loads(line) for output in outputs for line in output.splitlines()]


def refresh(
    slug: str, config: SwarmConfig, store: RedisStore, now_ms: int, run: Callable = subprocess.run
) -> list[str]:
    cached = {**EMPTY, **(get(store.redis, slug) or {})}
    if now_ms - cached["tried_at"] < REFRESH_MS:
        return []
    start_s = now_ms / 1000 - WINDOW_S
    known = cached.get("durations")
    since_s = start_s if known is None else max(start_s, cached["at"] / 1000 - OVERLAP_S)
    try:
        merged = {**(known or {}), **durations(read_runs(config.repo, since_s, run))}
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        print(f"ci speed kept its last value, reading Tests runs failed: {error}", file=sys.stderr)
        store.redis.set(key(slug), json.dumps({**cached, "tried_at": now_ms, "error": error}))
        return []
    window = {run_id: spent for run_id, spent in merged.items() if spent[0] >= start_s}
    record = {
        "minutes": median_minutes(window),
        "runs": len(window),
        "at": now_ms,
        "tried_at": now_ms,
        "durations": window,
    }
    store.redis.set(key(slug), json.dumps(record))
    return []
