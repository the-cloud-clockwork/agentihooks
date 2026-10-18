import json
from statistics import median

from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_events import iso_ms

GATE = "Gate — Required"
TIERS = {"pull_request": "pull request", "merge_group": "queue"}


def _wait(run, jobs, since_ms, now_ms):
    groups = {}
    active = set()
    for job in jobs:
        created = iso_ms(job["created_at"])
        queued = job["status"] not in {"in_progress", "completed"}
        started = iso_ms(job["started_at"]) if job.get("started_at") and not queued else now_ms
        elapsed = started - created
        if elapsed <= 300000:
            continue
        labels = tuple(sorted(set(job["labels"])))
        groups.setdefault(labels, []).append(elapsed)
        if queued or started >= since_ms:
            active.add(labels)
    found = []
    for labels, waits in groups.items():
        if labels not in active:
            continue
        longest = max(waits) // 1000
        typical = median(waits) / 1000
        text = ", ".join(labels) or "unassigned"
        found.append(
            Finding(
                "ci runner wait",
                f"{run['id']}-{run.get('run_attempt', 1)}-{json.dumps(labels)}",
                f"{text} runners waited up to {longest / 60:.2f} minutes for {len(waits)} jobs",
                (
                    f"run {run['id']}, labels {text}, {len(waits)} jobs, longest {longest} seconds, median {typical} seconds",
                ),
                "runner queue wait over 5 minutes",
                longest,
            )
        )
    return found


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
        found.extend(_wait(run, run["jobs"], since_ms, now_ms))
        found.extend(_wall(run, now_ms))
    return found
