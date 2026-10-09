"""Pull request Tests runs over fifteen minutes from push to Gate Required, each added once as a ledger follow up.

The minute tick calls it; CI runners cannot reach the ledger, so the defect is filed from the machine that can.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts import ci_budget
from scripts.swarm import ci_speed
from scripts.swarm.store import PREFIX

if TYPE_CHECKING:
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import RedisStore, SwarmConfig

REFRESH_MS = 5 * 60_000
WINDOW_S = 6 * 3600
SEEN_TTL_S = 2 * 24 * 3600
READ_ERRORS = (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError)
JOBS_JQ = ".jobs[] | {name, created_at, completed_at, conclusion, html_url} | @json"


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


def refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    now_ms: int,
    run: Callable = subprocess.run,
) -> list[str]:
    redis = store.redis
    if now_ms - int(redis.get(key(slug)) or 0) < REFRESH_MS:
        return []
    redis.set(key(slug), now_ms)
    try:
        runs = ci_speed.finished(ci_speed.read_runs(config.repo, now_ms / 1000 - WINDOW_S, run))
    except READ_ERRORS as exc:
        _skipped("reading Tests runs", exc)
        return []
    actions = []
    for item in runs:
        seen = key(slug, "seen", str(item.get("id")))
        try:
            spent = ci_budget.seconds(item["updated_at"]) - ci_budget.seconds(item["run_started_at"])
            if spent <= ci_budget.RUN_BUDGET_S or redis.exists(seen):
                continue
            text = defect(item, _jobs(config.repo, item["id"], run))
            if text and ledger.followup(slug, text) is False:
                continue
        except READ_ERRORS as exc:
            _skipped(f"Tests run {item.get('id')}", exc)
            continue
        if text:
            actions.append("filed a ledger follow up for a pull request Tests run over fifteen minutes")
        redis.set(seen, now_ms, ex=SEEN_TTL_S)
    return actions
