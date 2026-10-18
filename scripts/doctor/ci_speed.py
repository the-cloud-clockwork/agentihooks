from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_events import iso_ms

GATE = "Gate — Required"
TIERS = {"pull_request": "pull request", "merge_group": "queue"}


def _wait(run, job, since_ms, now_ms):
    created = iso_ms(job["created_at"])
    queued = job["status"] not in {"in_progress", "completed"}
    started = iso_ms(job["started_at"]) if job.get("started_at") and not queued else now_ms
    if not queued and created < since_ms and started < since_ms:
        return []
    elapsed = started - created
    if elapsed <= 300000:
        return []
    seconds = elapsed // 1000
    labels = ", ".join(job["labels"]) or "unassigned"
    return [
        Finding(
            "ci runner wait",
            f"{run['id']}-{run.get('run_attempt', 1)}-{job['id']}",
            f"{labels} runner waited {seconds / 60:.2f} minutes for {job['name']}",
            (f"run {run['id']}, job {job['id']} {job['name']}, labels {labels}, wait {seconds} seconds",),
            "runner queue wait over 5 minutes",
            seconds,
        )
    ]


def _wall(run, now_ms):
    event = run["event"]
    jobs = run["jobs"]
    if event not in TIERS or not jobs:
        return []
    gates = [job for job in jobs if job["name"] == GATE]
    if event == "pull_request" and not gates and run.get("path") != ".github/workflows/test.yml":
        return []
    completed = [iso_ms(job["completed_at"]) for job in gates if job.get("completed_at")]
    end = max(completed) if completed else now_ms
    if not completed and run["status"] == "completed":
        ends = [iso_ms(job["completed_at"]) for job in jobs if job.get("completed_at")]
        if not ends:
            return []
        end = max(ends)
    elapsed = end - min(iso_ms(job["created_at"]) for job in jobs)
    if elapsed <= 900000:
        return []
    seconds = elapsed // 1000
    return [
        Finding(
            "ci tier wall time",
            f"{run['id']}-{run.get('run_attempt', 1)}",
            f"{TIERS[event]} tier took {seconds / 60:.2f} minutes",
            (f"run {run['id']}, tier {event}, wall time {seconds} seconds",),
            "tier wall time over 15 minutes",
            seconds,
        )
    ]


def findings(records: list[dict], since_ms: int, now_ms: int) -> list[Finding]:
    found = []
    for run in records:
        for job in run["jobs"]:
            found.extend(_wait(run, job, since_ms, now_ms))
        found.extend(_wall(run, now_ms))
    return found
