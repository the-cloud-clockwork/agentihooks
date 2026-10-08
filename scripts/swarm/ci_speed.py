"""CI speed: the median wall time of completed Tests runs on pull requests into dev over the last day, cached in Redis
and read again once an hour."""

import json
import statistics
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from scripts.swarm.store import PREFIX

WORKFLOW = "test.yml"
WINDOW_S = 24 * 3600
REFRESH_MS = 3600 * 1000
FINISHED = {"success", "failure"}


def key(slug):
    return ":".join((PREFIX, slug, "ci-speed"))


def get(redis, slug) -> dict | None:
    raw = redis.get(key(slug))
    return json.loads(raw) if raw else None


def _seconds(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def finished(runs: list[dict]) -> list[dict]:
    """Run payloads mostly drop their pull request link, so the base is read from the head: every pull request into
    main comes from head dev and every other one goes into dev. A cancelled run never reached a verdict."""
    return [
        r
        for r in runs
        if r["event"] == "pull_request"
        and r["status"] == "completed"
        and r["conclusion"] in FINISHED
        and r["head_branch"] != "dev"
    ]


def median_minutes(runs: list[dict]) -> float | None:
    durations = [_seconds(r["updated_at"]) - _seconds(r["run_started_at"]) for r in finished(runs)]
    return round(statistics.median(durations) / 60, 2) if durations else None


def read_runs(repo_dir: str, now_ms: int, run: Callable = subprocess.run) -> list[dict]:
    since = datetime.fromtimestamp(now_ms / 1000 - WINDOW_S, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    endpoint = (
        f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs"
        f"?event=pull_request&status=completed&created=>={since}&per_page=100"
    )
    output = run(
        ["gh", "api", endpoint, "--paginate", "--jq", ".workflow_runs[] | @json"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [json.loads(line) for line in output.splitlines()]


def refresh(slug, config, store, now_ms, run: Callable = subprocess.run):
    cached = get(store.redis, slug) or {}
    if now_ms - cached.get("tried_at", 0) < REFRESH_MS:
        return []
    try:
        runs = read_runs(config.repo, now_ms, run)
        record = {"minutes": median_minutes(runs), "runs": len(finished(runs)), "at": now_ms, "tried_at": now_ms}
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        print(f"ci speed kept its last value, reading Tests runs failed: {error}", file=sys.stderr)
        store.redis.set(key(slug), json.dumps({**cached, "tried_at": now_ms, "error": error}))
        return []
    store.redis.set(key(slug), json.dumps(record))
    return []
