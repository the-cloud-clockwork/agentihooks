"""Dev promotion watch: the newest finished push run of Tests on dev files one urgent ci task per failed job and
raises a ledger alert; the next green run closes those tasks and the alert. A run whose only failure is the Sonar job
finding dev already moved past its commit is reported as superseded and files nothing.

The minute tick calls it; CI runners cannot reach the ledger, so the fixer tasks are filed from the machine that can.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.ci_budget import GATE
from scripts.swarm import dev_red, naming, promotion_owner
from scripts.swarm.store import PREFIX
from scripts.swarm_ledger import ledger_comments

if TYPE_CHECKING:
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import RedisStore, SwarmConfig

JOBS_JQ = ".jobs[] | {name, conclusion, check_run_url, steps} | @json"
ANNOTATIONS_JQ = ".[] | {annotation_level, message} | @json"
FAILED = ("failure", "timed_out")
SUPERSEDED_STEP = "Check dev still points at this commit"
SUPERSEDED = "Dev advanced before Sonar analysis; this unscanned revision cannot be promoted."
STEP_EXIT = "Process completed with exit code 1."
REFRESH_MS = 60_000
SOURCE = "promotion"
SENDER = "swarm"
TASK_PREFIX = "promo-"
UNNAMED = "Promotion red: a dev job"
NOT_DUPLICATE = "The promotion watch files one task per failed dev job and run, and holds a job while its task is open."
CLOSABLE = ("open", "blocked")
CLEARED = "A later dev push run of Tests passed, so the swarm closed this promotion task."


def key(slug: str, part: str) -> str:
    return ":".join((PREFIX, slug, SOURCE, part))


def _read(repo_dir: str, endpoint: str, jq: str, run: Callable) -> list[dict]:
    output = run(
        ["gh", "api", endpoint, "--paginate", "--jq", jq],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [json.loads(line) for line in output.splitlines() if line]


def jobs(repo_dir: str, run_id: int, run: Callable = subprocess.run) -> list[dict]:
    return _read(repo_dir, f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100", JOBS_JQ, run)


def superseded(repo_dir: str, job: dict, run: Callable = subprocess.run) -> bool:
    steps = [s["name"] for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
    if steps != [SUPERSEDED_STEP]:
        return False
    found = _read(repo_dir, f"{job['check_run_url']}/annotations?per_page=100", ANNOTATIONS_JQ, run)
    messages = {a["message"] for a in found if a["annotation_level"] == "failure"}
    return SUPERSEDED in messages and messages <= {SUPERSEDED, STEP_EXIT}


def failed_jobs(found: list[dict]) -> list[str]:
    failed = sorted({j["name"] for j in found if j["conclusion"] in FAILED})
    return [name for name in failed if name != GATE] or failed


def title(job: str) -> str:
    named = "Promotion red: " + " ".join(re.sub(r"[—–_/]", " ", job).split())
    return UNNAMED if ledger_comments.problems(named, "item") else named


def _promotion_tasks(doc: dict) -> list[dict]:
    return [t for t in doc.get("tasks", []) if t["id"].startswith(TASK_PREFIX)]


def _live_alerts(doc: dict) -> list[dict]:
    return [a for a in doc.get("alerts", []) if a.get("source") == SOURCE and a.get("state") != "done"]


def _held_jobs(redis, slug: str, doc: dict) -> set[str]:
    filed = redis.hgetall(key(slug, "jobs"))
    live = {t["id"] for t in _promotion_tasks(doc) if t.get("state") != "done"}
    if gone := [task_id for task_id in filed if task_id not in live]:
        redis.hdel(key(slug, "jobs"), *gone)
    return {job for task_id, job in filed.items() if task_id in live}


def _red(slug, redis, ledger, doc, last, failed, repo):
    run_id, sha, url = last["id"], last["head_sha"], last["html_url"]
    held, present = _held_jobs(redis, slug, doc), {t["id"] for t in doc.get("tasks", [])}
    for previous in promotion_owner.history(repo, redis) - {slug}:
        held.update(_held_jobs(redis, previous, ledger.state(previous)))
    actions = []
    for n, job in enumerate(failed):
        task_id = f"{TASK_PREFIX}{run_id}-{n}"
        if job in held or task_id in present:
            continue
        description = (
            f"The dev push run {run_id} of Tests failed in the job {job} at commit {sha}: {url}. Fix it on dev; "
            "the swarm closes this task when a later dev push run passes."
        )
        fields = {"task": task_id, "title": title(job), "lane": "ci", "kind": "ci", "rank": "urgent"}
        redis.hset(key(slug, "jobs"), task_id, job)
        ledger.add_task(slug, {**fields, "description": description, "not_duplicate": NOT_DUPLICATE}, SENDER)
        actions.append(f"filed {task_id} for the failed dev job {job}")
    if not _live_alerts(doc):
        where = f" in {', '.join(failed)}" if failed else ""
        ledger.raise_alert(
            slug, SOURCE, f"Dev promotion is red: push run {run_id} at commit {sha} failed{where}. {url}"
        )
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


def _recover(slug, redis, ledger, doc, last, repo):
    actions = _green(slug, ledger, doc, last)
    for previous in promotion_owner.history(repo, redis) - {slug}:
        actions.extend(_green(previous, ledger, ledger.state(previous), last))
    return actions


def _refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    doc: dict,
    now_ms: int,
    run: Callable = subprocess.run,
) -> list[str]:
    redis = store.redis
    if naming.PROOF_RE.fullmatch(slug) or now_ms - int(redis.get(key(slug, "checked")) or 0) < REFRESH_MS:
        return []
    redis.set(key(slug, "checked"), now_ms)
    try:
        last = dev_red.latest(config.repo, run)
        if last is None or int(redis.get(key(slug, "seen")) or 0) >= last["id"]:
            return []
        found = jobs(config.repo, last["id"], run) if last["conclusion"] == "failure" else []
        failed = failed_jobs(found)
        stale = bool(failed) and all(superseded(config.repo, j, run) for j in found if j["name"] in failed)
    except dev_red.READ_ERRORS as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        print(f"promotion watch skipped, reading dev push runs failed: {error}", file=sys.stderr)
        return []
    if stale:
        actions = [f"dev push run {last['id']} was superseded before Sonar analysis"]
    elif last["conclusion"] == "failure":
        actions = _red(slug, redis, ledger, doc, last, failed, config.repo)
    else:
        actions = _recover(slug, redis, ledger, doc, last, config.repo)
    redis.set(key(slug, "seen"), last["id"])
    return actions


def refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    doc: dict,
    now_ms: int,
    run: Callable = subprocess.run,
) -> list[str]:
    if naming.PROOF_RE.fullmatch(slug):
        return []
    with promotion_owner.intake(slug, config.repo, store) as owner:
        if owner != slug:
            return []
        record = promotion_owner.key(config.repo)
        seen = max(int(store.redis.hget(record, "seen") or 0), int(store.redis.get(key(slug, "seen")) or 0))
        if seen:
            store.redis.set(key(slug, "seen"), seen)
        if slug in store.slugs():
            doc = ledger.state(slug)
        actions = _refresh(slug, config, store, ledger, doc, now_ms, run)
        if handled := store.redis.get(key(slug, "seen")):
            store.redis.hset(record, "seen", handled)
        return actions
