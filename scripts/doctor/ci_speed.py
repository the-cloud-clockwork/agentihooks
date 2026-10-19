import json
from statistics import median

from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_events import iso_ms

GATE = "Gate — Required"
TIERS = {"pull_request": "pull request", "merge_group": "queue"}
COUNTED = {"success", "failure"}
SHORT_WAIT_MS = 300000
JOB_FIELDS = ("id", "name", "status", "labels", "created_at", "started_at", "completed_at", "runner_name")
SUMMARIES = {
    "ci runner wait": "{text} runners waited up to {minutes} minutes for {count} jobs",
    "ci queue age unconfirmed": "{text} jobs read as queued up to {minutes} minutes with no confirmed start, {count} jobs",
}
THRESHOLDS = {
    "ci runner wait": "runner queue wait over 5 minutes",
    "ci queue age unconfirmed": "unconfirmed queue age over 5 minutes",
}


def started(job: dict) -> bool:
    return bool(job.get("started_at")) and (
        job["status"] in {"in_progress", "completed"} or bool(job.get("runner_name"))
    )


def _subject(run, labels):
    return f"{run['id']}-{run.get('run_attempt', 1)}-{json.dumps(tuple(sorted(set(labels))))}"


def _wait(run, jobs, since_ms, now_ms):
    groups = {}
    active = set()
    for job in jobs:
        created = iso_ms(job["created_at"])
        began = iso_ms(job["started_at"]) if started(job) else now_ms
        elapsed = began - created
        if elapsed <= SHORT_WAIT_MS:
            continue
        kind = "ci runner wait" if started(job) else "ci queue age unconfirmed"
        key = (kind, tuple(sorted(set(job["labels"]))))
        groups.setdefault(key, []).append(elapsed)
        if not started(job) or began >= since_ms:
            active.add(key)
    found = []
    for (kind, labels), waits in groups.items():
        if (kind, labels) not in active:
            continue
        longest = max(waits) // 1000
        typical = median(waits) / 1000
        text = ", ".join(labels) or "unassigned"
        found.append(
            Finding(
                kind,
                _subject(run, labels),
                SUMMARIES[kind].format(text=text, minutes=f"{longest / 60:.2f}", count=len(waits)),
                (
                    f"run {run['id']}, labels {text}, {len(waits)} jobs, longest {longest} seconds, median {typical} seconds",
                ),
                THRESHOLDS[kind],
                longest,
            )
        )
    return found


def cleared(records: list[dict]) -> list[str]:
    groups = {}
    for run in records:
        for job in run["jobs"]:
            subject = _subject(run, job["labels"])
            began, short = groups.get(subject, (True, True))
            groups[subject] = (
                began and started(job),
                short and started(job) and iso_ms(job["started_at"]) - iso_ms(job["created_at"]) <= SHORT_WAIT_MS,
            )
    gone = []
    for subject, (began, short) in groups.items():
        kinds = [kind for kind in SUMMARIES if short or (began and kind != "ci runner wait")]
        gone.extend(f"{kind.replace(' ', '-')}/{subject}" for kind in kinds)
    return gone


def _wall(run, now_ms):
    event = run["event"]
    jobs = run["jobs"]
    if event not in TIERS or not jobs:
        return []
    if run["status"] == "completed" and run.get("conclusion") not in COUNTED:
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
            (
                f"run {run['id']}, tier {event}, conclusion {run.get('conclusion') or run['status']}, "
                f"wall time {seconds} seconds",
            ),
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
