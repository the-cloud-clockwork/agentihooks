"""Which held tasks have a pull request still waiting on its checks, so their idle ticks are not held against them."""

import subprocess

CACHE_SECONDS = 60


def pending(pr_url, run=subprocess.run):
    try:
        done = run(["gh", "pr", "checks", pr_url], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split("\t")[1:2] == ["pending"] for line in done.stdout.splitlines())


def cached(redis, prefix, run=subprocess.run):
    def probe(pr_url):
        key = f"{prefix}:{pr_url}"
        hit = redis.get(key)
        if hit is None:
            hit = "1" if pending(pr_url, run) else "0"
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
