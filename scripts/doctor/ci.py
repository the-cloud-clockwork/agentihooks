from scripts.swarm import ledger_events
from scripts.swarm.health.findings import Finding


def reruns(record: dict) -> list[Finding]:
    runs = [r for r in record["runs"] if r["head_sha"] == record["pr"]["head"]["sha"] and r["run_attempt"] > 1]
    count = sum(r["run_attempt"] - 1 for r in runs)
    if not count:
        return []
    return [
        Finding(
            "ci reruns",
            str(record["pr"]["number"]),
            f"{count} CI reruns on this pull request head",
            tuple(f"{r['id']} attempts {r['run_attempt']}" for r in runs),
            "at least one rerun",
            count,
        )
    ]


def red_checks(record: dict, now_ms: int) -> list[Finding]:
    checks = [
        c
        for c in record["checks"]
        if c["head_sha"] == record["pr"]["head"]["sha"]
        and c["conclusion"] in {"failure", "timed_out", "action_required", "startup_failure"}
    ]
    if not checks or now_ms - record["pushed_at"] < ledger_events.RED_QUIET_MS:
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


def findings(record: dict, now_ms: int) -> list[Finding]:
    return [*reruns(record), *red_checks(record, now_ms), *flaky_tests(record)]
