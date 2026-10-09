import json
import re
import subprocess
from collections.abc import Callable
from functools import partial

from scripts import ci_budget
from scripts.ci_budget import defects
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmConfig

REFRESH_MS = 60_000
PULL_URL = re.compile(r"https://github.com/([^/]+/[^/]+)/pull/([0-9]+)")


def api(repo_dir: str, endpoint: str, field: str, paginate: bool = True) -> list[dict]:
    command = ["gh", "api", endpoint, "--jq", f"{field} | @json"]
    if paginate:
        command.append("--paginate")
    result = subprocess.run(command, cwd=repo_dir, check=True, capture_output=True, text=True, timeout=60)
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def spent(repo: str, sha: str | None, event: str, merged_s: float, read: Callable) -> int | None:
    if not sha:
        return None
    prefix = f"repos/{repo}/actions"
    runs = read(f"{prefix}/workflows/test.yml/runs?event={event}&head_sha={sha}&per_page=100", ".workflow_runs[]")
    eligible = [
        run
        for run in runs
        if run["event"] == event and run["head_sha"] == sha and ci_budget.seconds(run["run_started_at"]) <= merged_s
    ]
    if not eligible:
        return None
    final = max(eligible, key=lambda run: (ci_budget.seconds(run["run_started_at"]), run["id"]))
    jobs = read(f"{prefix}/runs/{final['id']}/attempts/{final['run_attempt']}/jobs?per_page=100", ".jobs[]")
    gate = next((job for job in jobs if job["name"] == ci_budget.GATE), None)
    if not gate or gate["conclusion"] != "success" or not gate["completed_at"]:
        return None
    end = ci_budget.seconds(gate["completed_at"])
    if end > merged_s:
        return None
    begin = (
        ci_budget.seconds(final["created_at"])
        if final["run_attempt"] == 1
        else min(ci_budget.seconds(job["created_at"]) for job in jobs)
    )
    return round(end - begin)


def collect(repo: str, pull: dict, read: Callable) -> dict:
    merged = ci_budget.seconds(pull["merged_at"])
    head = spent(repo, pull["head"]["sha"], "pull_request", merged, read)
    queue = spent(repo, pull["merge_commit_sha"], "merge_group", merged, read)
    combined = head + queue if head is not None and queue is not None else None
    remaining = ci_budget.RUN_BUDGET_S - combined if combined is not None else None
    return {"head": head, "queue": queue, "combined": combined, "remaining": remaining}


def render(result: dict) -> str:
    def duration(value):
        if value is None:
            return "unknown"
        return f"{'minus ' if value < 0 else ''}{abs(value)} seconds"

    return (
        f"Delivery budget: head checks {duration(result['head'])}; queue checks {duration(result['queue'])}; "
        f"combined {duration(result['combined'])}; remaining {duration(result['remaining'])} "
        f"of {ci_budget.RUN_BUDGET_S} seconds."
    )


def recent(repo: str, since_s: float, read: Callable) -> list[dict]:
    found, page = [], 1
    while True:
        pulls = read(
            f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}",
            ".[]",
            paginate=False,
        )
        found.extend(pull for pull in pulls if pull["merged_at"] and ci_budget.seconds(pull["merged_at"]) >= since_s)
        if len(pulls) < 100 or ci_budget.seconds(pulls[-1]["updated_at"]) < since_s:
            return found
        page += 1


def refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    doc: dict,
    now_ms: int,
    read: Callable | None = None,
) -> list[str]:
    check_key = store.key(slug, "delivery-budget-check")
    if now_ms - int(store.redis.get(check_key) or 0) < REFRESH_MS:
        return []
    store.redis.set(check_key, now_ms)
    cursor_key = store.key(slug, "delivery-budget-cursor")
    since = (int(store.redis.get(cursor_key) or now_ms - ci_budget.RUN_BUDGET_S * 1000) - REFRESH_MS) / 1000
    tasks = {}
    for task in doc["tasks"]:
        match = PULL_URL.fullmatch(task.get("pr_url") or "")
        if match:
            tasks.setdefault(match[1], {}).setdefault(int(match[2]), []).append(task)
    read = read or partial(api, config.repo)
    actions = []
    try:
        for repo, rows in tasks.items():
            for pull in recent(repo, since, read):
                if pull["number"] not in rows:
                    continue
                text = render(collect(repo, pull, read))
                for task in rows[pull["number"]]:
                    if any(comment.get("text") == text for comment in task.get("comments", [])):
                        continue
                    ledger.comment(slug, task["id"], text, "swarm")
                    actions.append("recorded merged task delivery budget")
    except defects.READ_ERRORS as exc:
        defects._skipped("reading merged task delivery", exc)
        return actions
    store.redis.set(cursor_key, now_ms)
    return actions
