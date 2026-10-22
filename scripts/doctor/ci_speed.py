import json
from statistics import median

from scripts.doctor.words import plural
from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_events import iso_ms

GATE = "Gate — Required"
TIERS = {"pull_request": "pull request", "merge_group": "queue"}
COUNTED = {"success", "failure"}
SHORT_WAIT_MS = 300000
WALL_MS = 900000
UNCONFIRMED_WAIT_MS = SHORT_WAIT_MS + 600000
JOB_FIELDS = ("id", "name", "status", "conclusion", "labels", "created_at", "started_at", "completed_at", "runner_name")
SUMMARIES = {
    "ci runner wait": "{text} runners waited up to {minutes} minutes for {count} jobs",
    "ci queue age unconfirmed": "{text} jobs read as queued up to {minutes} minutes with no confirmed start, {count} jobs",
}
THRESHOLDS = {
    "ci runner wait": "runner queue wait over 5 minutes",
    "ci queue age unconfirmed": "unconfirmed queue age over 15 minutes",
    "ci tier wall time": "tier wall time over 15 minutes",
}
GROUPED = {
    "ci runner wait": "{text} runners waited up to {minutes} minutes in {count}",
    "ci queue age unconfirmed": "{text} jobs read as queued up to {minutes} minutes in {count}",
    "ci tier wall time": "{text} tier took up to {minutes} minutes in {count}",
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
        if elapsed <= (SHORT_WAIT_MS if started(job) else UNCONFIRMED_WAIT_MS):
            continue
        kind = "ci runner wait" if started(job) else "ci queue age unconfirmed"
        key = (kind, tuple(sorted(set(job["labels"]))))
        groups.setdefault(key, []).append(elapsed)
        if not started(job) or (run["status"] == "completed" and run.get("conclusion") in COUNTED):
            active.add(key)
    found = []
    for (kind, labels), waits in groups.items():
        if (kind, labels) not in active or (kind != "ci runner wait" and ("ci runner wait", labels) in groups):
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
            short, waited = groups.get(subject, (True, False))
            quick = started(job) and iso_ms(job["started_at"]) - iso_ms(job["created_at"]) <= SHORT_WAIT_MS
            groups[subject] = (short and quick, waited or (started(job) and not quick))
    gone = []
    for subject, (short, waited) in groups.items():
        kinds = [kind for kind in SUMMARIES if short or (waited and kind != "ci runner wait")]
        gone.extend(f"{kind.replace(' ', '-')}/{subject}" for kind in kinds)
    return gone


def wall_ms(run: dict, now_ms: int) -> int | None:
    event = run["event"]
    jobs = run["jobs"]
    if event not in TIERS or not jobs:
        return None
    if run["status"] != "completed" or run.get("conclusion") not in COUNTED:
        return None
    gates = [job for job in jobs if job["name"] == GATE]
    if event == "pull_request" and not gates and run.get("path") != ".github/workflows/test.yml":
        return None
    completed = [iso_ms(job["completed_at"]) for job in gates if job.get("completed_at")]
    end = max(completed) if completed else now_ms
    if not completed and run["status"] == "completed":
        ends = [iso_ms(job["completed_at"]) for job in jobs if job.get("completed_at")]
        if not ends:
            return None
        end = max(ends)
    return end - min(iso_ms(job["created_at"]) for job in jobs)


def _wall(run, now_ms):
    elapsed = wall_ms(run, now_ms)
    if elapsed is None or elapsed <= WALL_MS:
        return []
    event = run["event"]
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


def skipped(run: dict) -> list[Finding]:
    if run["event"] != "merge_group":
        return []
    delayed = []
    for job in run["jobs"]:
        if job.get("conclusion") != "skipped" or not job.get("started_at") or not job.get("completed_at"):
            continue
        lag = iso_ms(job["started_at"]) - iso_ms(job["completed_at"])
        if lag > SHORT_WAIT_MS:
            delayed.append((lag, job))
    if not delayed:
        return []
    lag, job = max(delayed, key=lambda entry: entry[0])
    seconds = lag // 1000
    attempt = run.get("run_attempt", 1)
    return [
        Finding(
            "ci skipped record lag",
            f"{run['id']}-{attempt}",
            f"{job['name']} skipped record appeared {seconds / 60:.2f} minutes after its skip decision",
            (
                f"run {run['id']} attempt {attempt}, job {job['name']}, started_at {job['started_at']}, "
                f"skip decision completed_at {job['completed_at']}, lag {seconds} seconds",
            ),
            "skipped record lag over 5 minutes",
            seconds,
        )
    ]


def findings(records: list[dict], since_ms: int, now_ms: int) -> list[Finding]:
    found = []
    for run in records:
        found.extend(_wait(run, run["jobs"], since_ms, now_ms))
        found.extend(_wall(run, now_ms))
        found.extend(skipped(run))
    return found


def _during(run, incidents):
    created = [iso_ms(job["created_at"]) for job in run["jobs"]]
    return sorted({name for start, end, name in incidents if any(start <= stamp <= end for stamp in created)})


def _member(groups, finding, scope, text, window_ms, run, during):
    identity = f"{finding.kind.replace(' ', '-')}/{scope}"
    group = groups.get(identity)
    if group is None or group["window"] != window_ms:
        group = groups[identity] = {
            "kind": finding.kind,
            "subject": scope,
            "text": text,
            "window": window_ms,
            "members": {},
        }
    attempt = run.get("run_attempt", 1)
    member = group["members"][finding.id] = {
        "order": [run["id"], attempt],
        "run": str(run["id"]) if attempt == 1 else f"{run['id']} attempt {attempt}",
        "measure": finding.measure,
        "evidence": finding.evidence[0],
        "during": during,
    }
    return member


def _running(run):
    return run["status"] != "completed" and not any(
        job.get("completed_at") for job in run["jobs"] if job["name"] == GATE
    )


def group(
    groups: dict, records: list[dict], since_ms: int, now_ms: int, window_ms: int, incidents: list[tuple]
) -> dict:
    """Adds each run's crossings to one group per class and label set or tier in the pass window."""
    for run in records:
        during = _during(run, incidents)
        for finding in _wait(run, run["jobs"], since_ms, now_ms):
            scope = finding.subject.split("-", 2)[2]
            text = ", ".join(json.loads(scope)) or "unassigned"
            member = _member(groups, finding, scope, text, window_ms, run, during)
            member["open"] = finding.kind != "ci runner wait"
        for finding in _wall(run, now_ms):
            member = _member(groups, finding, run["event"], TIERS[run["event"]], window_ms, run, during)
            member["open"] = _running(run)
    return groups


def prune(groups: dict, gone: list[str]) -> list[str]:
    """Drops superseded run crossings; returns the groups left with none."""
    emptied = []
    for identity, entry in list(groups.items()):
        for finding_id in gone:
            entry["members"].pop(finding_id, None)
        if not entry["members"]:
            del groups[identity]
            emptied.append(identity)
    return emptied


def grouped(groups: dict) -> list[Finding]:
    found = []
    for entry in groups.values():
        members = sorted(entry["members"].values(), key=lambda member: member["order"])
        measures = [member["measure"] for member in members]
        evidence = [
            f"{plural(len(members), 'run')}, {entry['text']}, worst {max(measures)} seconds, "
            f"median {median(measures)} seconds, runs {', '.join(member['run'] for member in members)}"
        ]
        during = [member for member in members if member["during"]]
        if during:
            names = sorted({name for member in during for name in member["during"]})
            evidence.append(
                f"{len(during)} of these runs had jobs created during the GitHub incident {'; '.join(names)}"
            )
        found.append(
            Finding(
                entry["kind"],
                entry["subject"],
                GROUPED[entry["kind"]].format(
                    text=entry["text"], minutes=f"{max(measures) / 60:.2f}", count=plural(len(members), "run")
                ),
                (*evidence, *(member["evidence"] for member in members)),
                THRESHOLDS[entry["kind"]],
                max(measures),
                evidence_identity=(
                    *[member["run"] for member in members],
                    max((member["measure"] for member in members if not member.get("open")), default=0),
                ),
            )
        )
    return found


def scope_of(subject: str) -> str:
    """The label set or tier of a grouped subject, without the window start older ids carry."""
    head, _, rest = subject.partition("-")
    return rest if head.isdigit() else subject


def _covered(finding, identity, verdict):
    kind, _, subject = identity.partition("/")
    if kind != finding.kind.replace(" ", "-") or scope_of(subject) != scope_of(finding.subject):
        return False
    identity = verdict.get("evidence_identity")
    members = identity[:-1] if identity is not None else verdict["evidence"][0].rpartition(", runs ")[2].split(", ")
    return finding.evidence_identity[-1] <= verdict["measure"] and set(finding.evidence_identity[:-1]).issubset(members)


def unjudged(found: list[Finding], records: dict, now_ms: int, cooldown_ms: int) -> list[Finding]:
    judgments = {identity: json.loads(raw)["verdict"] for identity, raw in records.items()}
    recent = {
        identity: verdict for identity, verdict in judgments.items() if verdict and now_ms < verdict["at"] + cooldown_ms
    }
    return [
        finding
        for finding in found
        if finding.evidence_identity is None
        or not any(_covered(finding, identity, verdict) for identity, verdict in recent.items())
    ]


def notice_key(finding: Finding, records: dict) -> str:
    prior = records.get(finding.id)
    if finding.evidence_identity is None or prior is None or not json.loads(prior)["verdict"]:
        return finding.id
    return json.dumps([finding.id, finding.evidence_identity])
