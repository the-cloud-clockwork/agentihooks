"""Pull request Tests runs over fifteen minutes from push to Gate Required, each added once as a ledger follow up.

The minute tick calls it; CI runners cannot reach the ledger, so the defect is filed from the machine that can.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from scripts import ci_budget
from scripts.swarm import ci_speed, metrics_ci, metrics_outbox
from scripts.swarm.store import PREFIX

if TYPE_CHECKING:
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import RedisStore, SwarmConfig

REFRESH_MS = 5 * 60_000
WINDOW_S = 6 * 3600
SEEN_TTL_S = 2 * 24 * 3600
METER_BATCH = 20
READ_ERRORS = (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError)
JOBS_JQ = ".jobs[] | {id, name, created_at, started_at, completed_at, conclusion, html_url} | @json"


def key(slug: str, *parts: str) -> str:
    return ":".join((PREFIX, slug, "ci-budget", *parts))


def _skipped(what: str, exc: Exception) -> None:
    error = getattr(exc, "stderr", None) or str(exc)
    print(f"ci budget skipped {what}: {error}", file=sys.stderr)


def _jobs(repo_dir: str, run_id: int, run: Callable) -> list[dict]:
    output = run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100",
            "--paginate",
            "--jq",
            JOBS_JQ,
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [json.loads(line) for line in output.splitlines() if line]


def _spoken(spent: float) -> str:
    minutes, rest = divmod(round(spent), 60)
    return f"{minutes} minutes {rest} seconds" if rest else f"{minutes} minutes"


def defect(item: dict, jobs: list[dict]) -> str | None:
    gate = next((j for j in jobs if j["name"] == ci_budget.GATE and j.get("completed_at")), None)
    if gate is None:
        return None
    spent = ci_budget.seconds(gate["completed_at"]) - ci_budget.seconds(item["run_started_at"])
    if spent <= ci_budget.RUN_BUDGET_S:
        return None
    stages = ci_budget.stages(jobs)
    stages.pop("gate-required", None)
    text = (
        f"A pull request Tests run took {_spoken(spent)} from push to Gate Required, "
        f"over the {ci_budget.RUN_BUDGET_S // 60} minute budget."
    )
    if stages:
        slowest = max(stages, key=stages.get)
        budget = ci_budget.BUDGETS.get(slowest)
        against = f"against {_spoken(budget)}" if budget else "with no budget"
        text += f" Its slowest stage {slowest} took {_spoken(stages[slowest])} {against}."
    return f"{text} {gate['html_url']}"


def _log(repo_dir: str, job_id: int, run: Callable) -> str:
    return run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def _meter(slug, redis, runs, now_ms, log_of, jobs_of, environ):
    batches = {metrics_ci.RUNS: [], metrics_ci.STAGES: [], metrics_ci.FAILURES: []}
    marks = []
    for item in runs:
        if len(marks) >= METER_BATCH:
            break
        try:
            mark = key(slug, "metered", str(item["id"]), str(item["run_attempt"]))
            if redis.exists(mark):
                continue
            jobs = jobs_of(item["id"])
            logs = {
                job["id"]: log_of(job["id"])
                for job in jobs
                if job.get("conclusion") == "failure" and job["name"] != ci_budget.GATE
            }
            found = metrics_ci.rows(slug, item, jobs, logs)
        except READ_ERRORS as exc:
            _skipped("metering one Tests run", exc)
            continue
        for table, table_rows in found.items():
            batches[table] += table_rows
        marks.append(mark)
    errors = metrics_ci.ship(now_ms, batches, environ)
    if not errors:
        for mark in marks:
            redis.set(mark, now_ms, ex=SEEN_TTL_S)
    return errors


def refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    now_ms: int,
    run: Callable = subprocess.run,
    environ: Mapping[str, str] = os.environ,
) -> list[str]:
    redis = store.redis
    if now_ms - int(redis.get(key(slug)) or 0) < REFRESH_MS:
        return []
    redis.set(key(slug), now_ms)
    since_s = now_ms / 1000 - WINDOW_S
    try:
        listed = ci_speed.read_runs(config.repo, since_s, run)
    except READ_ERRORS as exc:
        _skipped("reading Tests runs", exc)
        return []
    jobs = {}

    def jobs_of(run_id):
        if run_id not in jobs:
            jobs[run_id] = _jobs(config.repo, run_id, run)
        return jobs[run_id]

    actions = []
    for item in ci_speed.finished(listed):
        try:
            seen = key(slug, "seen", str(item["id"]))
            spent = ci_budget.seconds(item["updated_at"]) - ci_budget.seconds(item["run_started_at"])
            if spent <= ci_budget.RUN_BUDGET_S or redis.exists(seen):
                continue
            text = defect(item, jobs_of(item["id"]))
            if text and ledger.followup(slug, text) is not False:
                actions.append("filed a ledger follow up for a pull request Tests run over fifteen minutes")
        except READ_ERRORS as exc:
            _skipped("one Tests run", exc)
            continue
        redis.set(seen, now_ms, ex=SEEN_TTL_S)
    if metrics_outbox.settings(environ) is not None:
        try:
            listed = listed + ci_speed.read_runs(config.repo, since_s, run, event="push")
        except READ_ERRORS as exc:
            _skipped("reading dev push Tests runs", exc)
        runs = metrics_ci.metered(listed)
        actions += _meter(slug, redis, runs, now_ms, lambda job_id: _log(config.repo, job_id, run), jobs_of, environ)
    return actions
