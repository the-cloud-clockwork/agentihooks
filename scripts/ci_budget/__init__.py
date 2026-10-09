"""Each Tests stage's wall time, from its first job's creation to its last job's end, against its budget.

A stage's budget includes runner pickup, and the budgets along the longest chain of needs keep a pull request run
under fifteen minutes from push to Gate Required.
"""

import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime

RUN_BUDGET_S = 15 * 60
GATE = "Gate — Required"
SELF = "stage-budget"
BUDGETS = {
    "delivery-budget": 60,
    "durations": 60,
    "split": 120,
    "unit": 300,
    "shard-check": 90,
    "test-count": 120,
    "size": 90,
    "lint": 150,
    "sonar": 240,
    "coverage-ratchet": 90,
    "queue-baseline": 180,
    "mutation-plan": 90,
    "mutation": 480,
    "worker-image": 300,
    "swarm-image": 180,
    "semgrep": 120,
    "brain-smoke": 90,
    "kind-due": 60,
    "helm-kind": 480,
    "wiring": 60,
    "dependency-audit": 90,
    "gate-required": 60,
    "refresh-durations": 120,
    "coverage-baseline": 120,
    "stage-budget": 60,
}


def seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def stage_of(name: str) -> str:
    return "gate-required" if name == GATE else name.split(" / ")[0].split(" (")[0]


def stages(jobs: list[dict]) -> dict[str, float]:
    spans: dict[str, tuple[float, float]] = {}
    for job in jobs:
        stage = stage_of(job["name"])
        if job.get("conclusion") == "skipped" or not job.get("completed_at"):
            continue
        begin, end = seconds(job["created_at"]), seconds(job["completed_at"])
        first, last = spans.get(stage, (begin, end))
        spans[stage] = (min(first, begin), max(last, end))
    return {stage: end - begin for stage, (begin, end) in spans.items()}


def report(run: dict, jobs: list[dict], end_s: float) -> dict:
    rows = [
        {"stage": stage, "seconds": round(spent), "budget": BUDGETS.get(stage)}
        for stage, spent in sorted(stages(jobs).items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"total": round(end_s - seconds(run["run_started_at"])), "stages": rows}


def over(row: dict) -> bool:
    return row["budget"] is None or row["seconds"] > row["budget"]


def clock(spent: int) -> str:
    return f"{spent // 60}m{spent % 60:02d}s"


def verdict(row: dict) -> str:
    if row["budget"] is None:
        return "no budget"
    return "over" if over(row) else "ok"


def budget_cell(row: dict) -> str:
    return "none" if row["budget"] is None else clock(row["budget"])


def run_state(total: int) -> str:
    return "over" if total > RUN_BUDGET_S else "ok"


def render(result: dict) -> list[str]:
    lines = [f"{'stage':<20} {'wall':>7} {'budget':>7}  verdict"]
    for row in result["stages"]:
        lines.append(f"{row['stage']:<20} {clock(row['seconds']):>7} {budget_cell(row):>7}  {verdict(row)}")
    total = result["total"]
    lines.append(f"{'push to this report':<20} {clock(total):>7} {clock(RUN_BUDGET_S):>7}  {run_state(total)}")
    return lines


def delivery_api(endpoint: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def delivery_start(run: dict, jobs: list[dict]) -> float:
    if run.get("run_attempt", 1) == 1:
        return seconds(run["created_at"])
    return min(seconds(job["created_at"]) for job in jobs)


def delivery_end(jobs: list[dict]) -> float | None:
    return next(
        (seconds(job["completed_at"]) for job in jobs if job["name"] == GATE and job.get("completed_at")),
        None,
    )


def delivery_head(run: dict, repo: str, api: Callable[[str], list[dict]] | None = None) -> dict | None:
    if run.get("event") != "merge_group":
        return None
    match = re.fullmatch(r"(?:refs/heads/)?gh-readonly-queue/.+/pr-([0-9]+)-[0-9a-f]+", run["head_branch"])
    if not match:
        return None
    read = api or delivery_api
    prefix = f"repos/{repo}"
    head = read(f"{prefix}/pulls/{match[1]}")[0]["head"]["sha"]
    pages = read(
        f"{prefix}/actions/workflows/{run['workflow_id']}/runs?event=pull_request&head_sha={head}&per_page=100"
    )
    candidates = [
        item
        for page in pages
        for item in page["workflow_runs"]
        if item["head_sha"] == head
        and item["event"] == "pull_request"
        and item["status"] == "completed"
        and item["conclusion"] == "success"
    ]
    if not candidates:
        return None
    final = max(candidates, key=lambda item: (seconds(item["run_started_at"]), item["id"]))
    pages = read(f"{prefix}/actions/runs/{final['id']}/attempts/{final['run_attempt']}/jobs?per_page=100")
    gate = next(
        (job for page in pages for job in page["jobs"] if job["name"] == GATE),
        None,
    )
    if not gate or gate["conclusion"] != "success" or not gate["completed_at"]:
        return None
    end = seconds(gate["completed_at"])
    if end > seconds(run["created_at"]):
        return None
    jobs = [job for page in pages for job in page["jobs"]]
    return {"run": final["id"], "seconds": round(end - delivery_start(final, jobs))}


def delivery_report(run: dict, head: dict | None, end_s: float, jobs: list[dict] | None = None) -> dict:
    listed = jobs or []
    finished = delivery_end(listed)
    queue = round((finished if finished is not None else end_s) - delivery_start(run, listed))
    spent = head["seconds"] if head is not None else None
    combined = spent + queue if spent is not None else None
    remaining = RUN_BUDGET_S - combined if combined is not None else None
    return {"head": spent, "queue": queue, "combined": combined, "remaining": remaining}


def delivery_rows(result: dict, final: bool = False) -> list[tuple[str, str]]:
    labels = {
        "head": "final head checks",
        "queue": "queue checks" if final else "queue checks so far",
        "combined": "combined delivery",
        "remaining": "remaining delivery",
    }
    return [
        (labels[key], "unknown" if spent is None else ("-" if spent < 0 else "") + clock(abs(spent)))
        for key, spent in result.items()
    ]
