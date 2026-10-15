"""Dev promotion watch: the newest finished push run of Tests on dev files one urgent ci task per failed job and
raises a ledger alert; the next green run closes those tasks and the alert.

The minute tick calls it; CI runners cannot reach the ledger, so the fixer tasks are filed from the machine that can.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable

from scripts.ci_budget import GATE
from scripts.swarm.ci_speed import FINISHED, WORKFLOW
from scripts.swarm.store import PREFIX

RUNS = f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs?branch=dev&event=push&status=completed&per_page=20"
RUNS_JQ = ".workflow_runs[] | {id, conclusion, head_sha, html_url} | @json"
JOBS_JQ = ".jobs[] | {name, conclusion} | @json"
REFRESH_MS = 60_000
SOURCE = "promotion"
SENDER = "swarm"
TASK_PREFIX = "promo-"
CLOSABLE = ("open", "blocked")
CLEARED = "A later dev push run of Tests passed, so the swarm closed this promotion task."
READ_ERRORS = (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError)


def key(slug: str, part: str) -> str:
    return ":".join((PREFIX, slug, SOURCE, part))


def _gh(repo_dir: str, args: list[str], run: Callable) -> list[dict]:
    output = run(["gh", "api", *args], cwd=repo_dir, capture_output=True, text=True, check=True, timeout=60).stdout
    return [json.loads(line) for line in output.splitlines() if line]


def latest(repo_dir: str, run: Callable = subprocess.run) -> dict | None:
    runs = _gh(repo_dir, [RUNS, "--jq", RUNS_JQ], run)
    return max((r for r in runs if r["conclusion"] in FINISHED), key=lambda r: r["id"], default=None)


def jobs(repo_dir: str, run_id: int, run: Callable = subprocess.run) -> list[dict]:
    endpoint = f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100"
    return _gh(repo_dir, [endpoint, "--paginate", "--jq", JOBS_JQ], run)


def failed_jobs(found: list[dict]) -> list[str]:
    failed = sorted({j["name"] for j in found if j["conclusion"] == "failure"})
    return [name for name in failed if name != GATE] or failed


def title(job: str) -> str:
    return "Promotion red: " + " ".join(re.sub(r"[—–]", " ", job).split())


def _promotion_tasks(doc: dict) -> list[dict]:
    return [t for t in doc.get("tasks", []) if t["id"].startswith(TASK_PREFIX)]


def _live_alerts(doc: dict) -> list[dict]:
    return [a for a in doc.get("alerts", []) if a.get("source") == SOURCE and a.get("state") != "done"]


def _red(slug, ledger, doc, last, failed):
    run_id, sha, url = last["id"], last["head_sha"], last["html_url"]
    held = {t["title"] for t in _promotion_tasks(doc) if t.get("state") != "done"}
    actions = []
    for n, job in enumerate(failed):
        if title(job) in held:
            continue
        task_id = f"{TASK_PREFIX}{run_id}-{n}"
        description = (
            f"The dev push run {run_id} of Tests failed in the job {job} at commit {sha}: {url}. Fix it on dev; "
            "the swarm closes this task when a later dev push run passes."
        )
        fields = {"task": task_id, "title": title(job), "lane": "ci", "kind": "ci", "rank": "urgent"}
        ledger.add_task(slug, {**fields, "description": description}, SENDER)
        held.add(title(job))
        actions.append(f"filed {task_id} for the failed dev job {job}")
    if not _live_alerts(doc):
        text = f"Dev promotion is red: push run {run_id} at commit {sha} failed in {', '.join(failed)}. {url}"
        ledger.raise_alert(slug, SOURCE, text)
        actions.append(f"raised the promotion alert for dev push run {run_id}")
    return actions


def _green(slug, ledger, doc, last):
    run_id, actions = last["id"], []
    for task in [t for t in _promotion_tasks(doc) if t.get("state") in CLOSABLE]:
        live = ledger.update_task(slug, task["id"], {"state": "done"}, by=SENDER, if_state=CLOSABLE)
        if live.get("state") == "done":
            ledger.comment(slug, task["id"], CLEARED, by=SENDER)
            actions.append(f"closed {task['id']}, dev push run {run_id} passed")
    for alert in _live_alerts(doc):
        ledger.close_alert(slug, alert["id"], f"Dev push run {run_id} of Tests passed.")
        actions.append(f"cleared the promotion alert, dev push run {run_id} passed")
    return actions


def refresh(slug, config, store, ledger, doc, now_ms, run: Callable = subprocess.run) -> list[str]:
    redis = store.redis
    if now_ms - int(redis.get(key(slug, "checked")) or 0) < REFRESH_MS:
        return []
    redis.set(key(slug, "checked"), now_ms)
    try:
        last = latest(config.repo, run)
        if last is None or int(redis.get(key(slug, "seen")) or 0) >= last["id"]:
            return []
        failed = failed_jobs(jobs(config.repo, last["id"], run)) if last["conclusion"] == "failure" else []
    except READ_ERRORS as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        print(f"promotion watch skipped, reading dev push runs failed: {error}", file=sys.stderr)
        return []
    if last["conclusion"] == "failure":
        actions = _red(slug, ledger, doc, last, failed)
    else:
        actions = _green(slug, ledger, doc, last)
    redis.set(key(slug, "seen"), last["id"])
    return actions
