"""Which held tasks have a pull request waiting on checks or operator approval."""

import subprocess

CACHE_SECONDS = 60


def pending(pr_url, run=subprocess.run, approval=False):
    try:
        done = run(["gh", "pr", "checks", pr_url], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    states = [line.split("\t")[1:2] for line in done.stdout.splitlines()]
    return any(state == ["pending"] for state in states) or (
        approval and done.returncode == 0 and bool(states) and all(state == ["pass"] for state in states)
    )


def cached(redis, prefix, run=subprocess.run, approval=False):
    def probe(pr_url):
        key = f"{prefix}:approval:{pr_url}" if approval else f"{prefix}:{pr_url}"
        hit = redis.get(key)
        if hit is None:
            hit = "1" if pending(pr_url, run, approval=approval) else "0"
            redis.set(key, hit, ex=CACHE_SECONDS)
        return hit == "1"

    return probe


def waiting(agents, tasks, limits, probe=pending):
    rows = {t["id"]: t for t in tasks}
    found = set()
    for a in agents:
        row = rows.get(a.get("task"), {})
        if a.get("idle_ticks", 0) < limits.idle_ticks or row.get("state") != "pr" or not row.get("pr_url"):
            continue
        if probe(row["pr_url"]):
            found.add(row["id"])
    return found
