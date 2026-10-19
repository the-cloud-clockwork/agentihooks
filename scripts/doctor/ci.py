from scripts.gates.reruns import RerunBudget
from scripts.swarm import ledger_events
from scripts.swarm.ci_speed import WORKFLOW
from scripts.swarm.health.findings import Finding

DEV_STREAK = 5
DEV_FAILED = {"failure", "timed_out", "startup_failure"}
DEV_FINISHED = DEV_FAILED | {"success"}


def reruns(record: dict) -> list[Finding]:
    runs = [r for r in record["runs"] if r["head_sha"] == record["pr"]["head"]["sha"] and r["run_attempt"] > 1]
    count = sum(r["run_attempt"] - 1 for r in runs)
    cap = RerunBudget().cap
    if count <= cap:
        return []
    return [
        Finding(
            "ci reruns",
            str(record["pr"]["number"]),
            f"{count} CI reruns on this pull request head",
            tuple(f"{r['id']} attempts {r['run_attempt']}" for r in runs),
            f"more than {cap} reruns per pull request head",
            count,
        )
    ]


def red_checks(record: dict, now_ms: int) -> list[Finding]:
    sha = record["pr"]["head"]["sha"]
    checks = [
        c
        for c in record["checks"]
        if c["head_sha"] == sha and c["conclusion"] in {"failure", "timed_out", "action_required", "startup_failure"}
    ]
    pushes = [ledger_events.iso_ms(r["created_at"]) for r in record["runs"] if r["head_sha"] == sha]
    reds = [ledger_events.iso_ms(c["completed_at"]) for c in checks]
    if not checks or ledger_events.red_window(min(pushes, default=None), min(reds), now_ms) is None:
        return []
    return [
        Finding(
            "red checks",
            str(record["pr"]["number"]),
            f"{len(checks)} red checks on this pull request head",
            tuple(f"{c['name']}: {c['conclusion']} {c['html_url']}" for c in checks),
            "at least one red check",
            len(checks),
        )
    ]


def flaky_tests(record: dict) -> list[Finding]:
    observations = {}
    sha = record["pr"]["head"]["sha"]
    for attempt in record["attempts"]:
        if attempt["head_sha"] != sha:
            continue
        for test in attempt["tests"]:
            key = (attempt["run_id"], test["job"], test["nodeid"])
            observations.setdefault(key, {}).setdefault(attempt["attempt"], set()).add(test["outcome"])
    found = []
    for (run_id, job, nodeid), attempts in sorted(observations.items()):
        failed = [n for n, outcomes in attempts.items() if "FAILED" in outcomes]
        passed = [n for n, outcomes in attempts.items() if outcomes == {"PASSED"}]
        transitions = [(f, p) for f in failed for p in passed if p > f]
        if transitions:
            first, second = min(transitions)
            found.append(
                Finding(
                    "flaky tests",
                    f"{record['pr']['number']}/{run_id}/{job}/{nodeid}",
                    "test failed then passed on the same commit and job",
                    (
                        nodeid,
                        f"head {sha}",
                        f"run {run_id}, job {job}",
                        f"attempt {first} FAILED, attempt {second} PASSED",
                    ),
                    "failure followed by pass on unchanged code",
                    1,
                )
            )
    return found


def red_streaks(runs: list[dict]) -> dict[str, list[dict]]:
    by_path = {}
    for found in sorted(runs, key=lambda r: -r["id"]):
        if found["conclusion"] in DEV_FINISHED and found["path"].split("/")[-1] != WORKFLOW:
            by_path.setdefault(found["path"], []).append(found)
    streaks = {}
    for path, finished in by_path.items():
        streak = next((finished[:n] for n, r in enumerate(finished) if r["conclusion"] not in DEV_FAILED), finished)
        if len(streak) >= DEV_STREAK:
            streaks[path] = streak
    return streaks


def red_dev_workflows(record: dict) -> list[Finding]:
    found = []
    for path, streak in red_streaks(record["runs"]).items():
        latest, green = streak[0], record["green"].get(path)
        jobs = record["jobs"].get(latest["id"]) or ["none read"]
        found.append(
            Finding(
                "red dev workflow",
                path.split("/")[-1],
                f"{latest['name']} failed its last {len(streak)} dev push runs",
                (
                    f"last green {green['created_at']} {green['html_url']}" if green else "no green dev push run found",
                    f"failing jobs in run {latest['id']}: {', '.join(jobs)}",
                    *(r["html_url"] for r in streak),
                ),
                f"{DEV_STREAK} consecutive failed dev push runs outside the promotion gate",
                len(streak),
            )
        )
    return found


def findings(record: dict, now_ms: int) -> list[Finding]:
    return [*reruns(record), *red_checks(record, now_ms), *flaky_tests(record)]
