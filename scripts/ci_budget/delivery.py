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


def _pending(slug, store, doc):
    reported = store.key(slug, "delivery-budget-reported")
    pending = {}
    for task in reversed(doc["tasks"]):
        url = task.get("pr_url") or ""
        match = PULL_URL.fullmatch(url)
        binding = f"{task['id']}:{url}"
        if match and not store.redis.hexists(reported, binding):
            pending[binding] = (match[1], int(match[2]), task)
    return pending


def _selection(slug, store, pending):
    known = store.key(slug, "delivery-budget-known")
    watching = store.key(slug, "delivery-budget-watching")
    if not store.redis.exists(known):
        store.redis.sadd(known, "initialized", *pending)
    fresh = [binding for binding in pending if not store.redis.sismember(known, binding)]
    live = [
        binding
        for binding, (_, _, task) in pending.items()
        if task.get("state") == "pr" or store.redis.sismember(watching, binding)
    ]
    backlog = [binding for binding in pending if binding not in fresh and binding not in live]
    offset_key = store.key(slug, "delivery-budget-offset")
    offset = int(store.redis.get(offset_key) or 0)
    offset = offset % len(backlog) if backlog else 0
    batch = (backlog + backlog)[offset : offset + 8]
    store.redis.set(offset_key, offset + len(batch))
    return list(dict.fromkeys([*live, *fresh[:8], *batch]))


def _record(slug, store, ledger, binding, details, pull, read):
    repo, _, task = details
    known = store.key(slug, "delivery-budget-known")
    watching = store.key(slug, "delivery-budget-watching")
    if not pull["merged_at"]:
        store.redis.sadd(known, binding)
        if pull.get("state") == "open":
            store.redis.sadd(watching, binding)
        else:
            store.redis.srem(watching, binding)
        return False
    result = collect(repo, pull, read)
    text = render(result)
    saved = any(comment.get("text") == text for comment in task.get("comments", []))
    if not saved and not ledger.delivery_budget(slug, task["id"], text):
        store.redis.sadd(watching, binding)
        return False
    store.redis.hset(store.key(slug, "delivery-budget-reported"), binding, json.dumps(result))
    store.redis.sadd(known, binding)
    store.redis.srem(watching, binding)
    return not saved


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
    pending = _pending(slug, store, doc)
    selected = _selection(slug, store, pending)
    read = read or partial(api, config.repo)
    actions = []
    for binding in selected:
        repo, number, task = pending[binding]
        try:
            pull = read(f"repos/{repo}/pulls/{number}", ".")[0]
            if _record(slug, store, ledger, binding, pending[binding], pull, read):
                actions.append("recorded merged task delivery budget")
        except defects.READ_ERRORS as exc:
            defects._skipped("reading merged task delivery", exc)
    return actions
