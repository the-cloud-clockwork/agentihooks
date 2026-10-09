"""Each Tests stage's wall time, from its first job's creation to its last job's end, against its budget.

A stage's budget includes runner pickup, and the budgets along the longest chain of needs keep a pull request run
under fifteen minutes from push to Gate Required.
"""

from datetime import datetime

RUN_BUDGET_S = 15 * 60
GATE = "Gate — Required"
SELF = "stage-budget"
BUDGETS = {
    "reuse": 30,
    "durations": 60,
    "split": 120,
    "unit": 300,
    "shard-check": 90,
    "test-count": 120,
    "lint": 240,
    "sonar": 240,
    "coverage-ratchet": 90,
    "queue-baseline": 180,
    "mutation-plan": 90,
    "mutation": 480,
    "worker-image": 300,
    "swarm-image": 180,
    "semgrep": 120,
    "ledger-load": 240,
    "brain-smoke": 90,
    "kind-due": 60,
    "helm-kind": 480,
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
