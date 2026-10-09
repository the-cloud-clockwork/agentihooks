import sqlite3

from scripts import ci_budget
from scripts.doctor import ci_read
from scripts.swarm import ci_speed, metrics_outbox, test_signatures

RUN_KEYS = (("run_id", "Int64"), ("attempt", "Int64"))
RUNS = metrics_outbox.Table(
    "ci_runs",
    (
        ("workflow", "String"),
        ("job", "String"),
        ("event", "String"),
        ("branch", "String"),
        ("head_sha", "String"),
        *RUN_KEYS,
        ("conclusion", "String"),
        ("queue_s", "Float64"),
        ("run_s", "Float64"),
    ),
)
STAGES = metrics_outbox.Table(
    "ci_stages",
    (*RUN_KEYS, ("stage", "String"), ("seconds", "Float64"), ("budget", "Int64"), ("pickup_s", "Float64")),
)
FAILURES = metrics_outbox.Table(
    "ci_failures",
    (
        *RUN_KEYS,
        ("job", "String"),
        ("branch", "String"),
        ("head_sha", "String"),
        ("test_id", "String"),
        ("message", "String"),
        ("signature", "String"),
    ),
)


def metered(runs: list[dict]) -> list[dict]:
    pushes = [
        r
        for r in runs
        if r["event"] == "push"
        and r["head_branch"] == "dev"
        and r["status"] == "completed"
        and r["conclusion"] in ci_speed.FINISHED
    ]
    return ci_speed.finished(runs) + pushes


def _base(slug, item, ts_ms, event_id):
    path = dict.fromkeys(("plan", "phase", "slice", "task"), "")
    return {"event_id": event_id, "ledger": slug, "ts_ms": ts_ms, **path, "run_id": item["id"]}


def _failures(log, job):
    found = {}
    for test in ci_read.test_results(log, job):
        if test["outcome"] == "FAILED":
            test_id, _, message = test["nodeid"].partition(" - ")
            found[test_id] = message or found.get(test_id, "")
    return found


def _pickups(jobs):
    pickups = {}
    for job in jobs:
        if job.get("started_at") and job.get("conclusion") != "skipped":
            stage = ci_budget.stage_of(job["name"])
            wait = ci_budget.seconds(job["started_at"]) - ci_budget.seconds(job["created_at"])
            pickups[stage] = max(pickups.get(stage, 0.0), wait)
    return pickups


def rows(slug: str, item: dict, jobs: list[dict], logs: dict) -> dict:
    gate = next((j for j in jobs if j["name"] == ci_budget.GATE and j.get("completed_at")), None)
    end = gate["completed_at"] if gate else item["updated_at"]
    ts_ms = round(ci_budget.seconds(end) * 1000)
    started = ci_budget.seconds(item["run_started_at"])
    attempt = item["run_attempt"]
    key = f"{item['id']}:{attempt}"
    run_row = {
        **_base(slug, item, ts_ms, f"ci-run:{key}"),
        "workflow": ci_speed.WORKFLOW,
        "job": gate["name"] if gate else "",
        "event": item["event"],
        "branch": item["head_branch"],
        "head_sha": item["head_sha"],
        "attempt": attempt,
        "conclusion": item["conclusion"],
        "queue_s": started - ci_budget.seconds(item["created_at"]),
        "run_s": ci_budget.seconds(end) - started,
    }
    pickups = _pickups(jobs)
    stage_rows = [
        {
            **_base(slug, item, ts_ms, f"ci-stage:{key}:{stage}"),
            "attempt": attempt,
            "stage": stage,
            "seconds": spent,
            "budget": ci_budget.BUDGETS.get(stage, 0),
            "pickup_s": pickups.get(stage, 0.0),
        }
        for stage, spent in ci_budget.stages(jobs).items()
    ]
    failure_rows = [
        {
            **_base(slug, item, ts_ms, f"ci-fail:{key}:{job['id']}:{test_id}"),
            "attempt": attempt,
            "job": job["name"],
            "branch": item["head_branch"],
            "head_sha": item["head_sha"],
            "test_id": test_id,
            "message": message,
            "signature": test_signatures.signature(test_id, message),
        }
        for job in jobs
        if job.get("id") in logs
        for test_id, message in _failures(logs[job["id"]], job["name"]).items()
    ]
    found = {RUNS: [run_row], STAGES: stage_rows, FAILURES: failure_rows}
    for table, table_rows in found.items():
        for row in table_rows:
            table.check(row)
    return found


def ship(now_ms: int, batches: dict, environ) -> list[str]:
    sink = metrics_outbox.settings(environ)
    if sink is None:
        return []
    try:
        box = metrics_outbox.Outbox(metrics_outbox.spool_path(), sink)
        try:
            for table, found in batches.items():
                box.append(table, found)
            box.flush(now_ms)
        finally:
            box.close()
    except (sqlite3.Error, OSError) as exc:
        return [f"metrics outbox failed: {exc}"]
    return []
