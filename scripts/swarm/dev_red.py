"""Tasks blocked because dev went red: swarm block records the failing dev Tests run, and the tick reopens each one
once a later dev Tests run passes."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable

from scripts.swarm.ci_speed import FINISHED, WORKFLOW
from scripts.swarm.store import PREFIX

ENDPOINT = (
    f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs?branch=dev&event=push&status=completed&per_page=20"
)
JQ = ".workflow_runs[] | {id, conclusion} | @json"
REOPENED = "Dev Tests passed again after the red run that blocked this task, so the swarm reopened it."


def key(slug: str) -> str:
    return ":".join((PREFIX, slug, "dev-red"))


def latest(repo_dir: str, run: Callable = subprocess.run) -> dict | None:
    output = run(
        ["gh", "api", ENDPOINT, "--jq", JQ], cwd=repo_dir, capture_output=True, text=True, check=True, timeout=60
    ).stdout
    runs = [json.loads(line) for line in output.splitlines()]
    return max((r for r in runs if r["conclusion"] in FINISHED), key=lambda r: r["id"], default=None)


def record(redis, slug: str, task_id: str, repo_dir: str, run: Callable = subprocess.run) -> int | None:
    last = latest(repo_dir, run)
    if last is None or last["conclusion"] != "failure":
        return None
    redis.hset(key(slug), task_id, str(last["id"]))
    return last["id"]


def clear(redis, slug: str, task_id: str) -> None:
    redis.hdel(key(slug), task_id)


def reopen_pass(slug, config, store, ledger, rows, run: Callable = subprocess.run) -> list[str]:
    held = store.redis.hgetall(key(slug))
    if not held:
        return []
    try:
        last = latest(config.repo, run)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        print(f"dev red reopen skipped, reading dev Tests runs failed: {error}", file=sys.stderr)
        return []
    green = last["id"] if last and last["conclusion"] == "success" else 0
    actions = []
    for task_id, red in held.items():
        if rows.get(task_id, {}).get("state") != "blocked":
            clear(store.redis, slug, task_id)
        elif green > int(red):
            live = ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""}, if_state=("blocked",))
            rows[task_id].update(live)
            clear(store.redis, slug, task_id)
            if live.get("state") == "open":
                ledger.comment(slug, task_id, REOPENED, by="swarm")
                actions.append(f"task {task_id} reopened, dev Tests passed after the red run that blocked it")
    return actions
