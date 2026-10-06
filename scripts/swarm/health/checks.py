"""Which held tasks have a pull request waiting on checks or operator approval."""

import subprocess

CACHE_SECONDS = 60


def _states(pr_url, run):
    try:
        done = run(["gh", "pr", "checks", pr_url], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None, []
    return done.returncode, [line.split("\t")[1:2] for line in done.stdout.splitlines()]


def pending(pr_url, run=subprocess.run, approval=False):
    code, states = _states(pr_url, run)
    return any(state == ["pending"] for state in states) or (
        approval and code == 0 and bool(states) and all(state == ["pass"] for state in states)
    )


def passing(pr_url, run=subprocess.run):
    code, states = _states(pr_url, run)
    return code == 0 and ["pass"] in states and all(state in (["pass"], ["skipping"]) for state in states)


def _memo(redis, key, compute):
    hit = redis.get(key)
    if hit is None:
        hit = "1" if compute() else "0"
        redis.set(key, hit, ex=CACHE_SECONDS)
    return hit == "1"


def cached(redis, prefix, run=subprocess.run, approval=False):
    def probe(pr_url):
        key = f"{prefix}:approval:{pr_url}" if approval else f"{prefix}:{pr_url}"
        return _memo(redis, key, lambda: pending(pr_url, run, approval=approval))

    return probe


def cached_green(redis, prefix, run=subprocess.run):
    return lambda pr_url: _memo(redis, f"{prefix}:green:{pr_url}", lambda: passing(pr_url, run))


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


def green(tasks, probe=passing):
    return {t["id"] for t in tasks if t.get("state") == "pr" and t.get("pr_url") and probe(t["pr_url"])}
