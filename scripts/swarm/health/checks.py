"""Which held tasks have a pull request still waiting on its checks, so their idle ticks are not held against them."""

import subprocess


def pending(pr_url, run=subprocess.run):
    try:
        done = run(["gh", "pr", "checks", pr_url], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split("\t")[1:2] == ["pending"] for line in done.stdout.splitlines())


def waiting(agents, tasks, limits, run=subprocess.run):
    rows = {t["id"]: t for t in tasks}
    found = set()
    for a in agents:
        row = rows.get(a.get("task"), {})
        if a.get("idle_ticks", 0) < limits.idle_ticks or row.get("state") != "pr" or not row.get("pr_url"):
            continue
        if pending(row["pr_url"], run):
            found.add(row["id"])
    return found
