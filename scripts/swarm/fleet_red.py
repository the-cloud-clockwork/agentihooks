"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable

from scripts.swarm.ledger_events import RED, iso_ms
from scripts.tick.adapters.github import command

MIN_OTHERS = 3
PULL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/\d+")
WINDOW_MS = 6 * 3_600_000
ERRORS = (OSError, ValueError, subprocess.SubprocessError)
NOTICE = (
    "The swarm holds this task unclaimed: its pull request {url} is red only from {checks}, which also fail on "
    "most other open pull requests. It is claimed again once those checks stop failing elsewhere."
)


class ReadError(Exception):
    pass


def failing(pull: dict) -> set[str]:
    return {
        check.get("name") or check.get("context")
        for check in pull.get("statusCheckRollup") or []
        if (check.get("conclusion") or check.get("state")) in RED
    }


def own(pulls: list[dict], url: str) -> set[str]:
    return next((failing(p) for p in pulls if p.get("url") == url), set())


def recent(pull: dict, now_ms: int) -> bool:
    marks = [c.get("completedAt") or c.get("startedAt") for c in pull.get("statusCheckRollup") or []]
    return any(iso_ms(mark) >= now_ms - WINDOW_MS for mark in marks if mark)


def repository(url: str) -> str:
    found = PULL.match(url)
    return found.group(1) if found else ""


def read(repo: str, run: Callable = subprocess.run) -> list[dict]:
    found = run(
        command(["pr", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", "url,statusCheckRollup"]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if found.returncode:
        raise ReadError(found.stderr.strip() or f"gh pr list exited {found.returncode}")
    return json.loads(found.stdout)


def shared(pulls: list[dict], url: str, now_ms: int) -> set[str]:
    others = [failing(p) for p in pulls if p.get("url") != url and recent(p, now_ms)]
    if len(others) < MIN_OTHERS:
        return set()
    return {name for name in own(pulls, url) if sum(name in other for other in others) * 2 > len(others)}


def held(pulls: list[dict], url: str, now_ms: int) -> bool:
    mine = own(pulls, url)
    return bool(mine) and mine <= shared(pulls, url, now_ms)


def checker(reader: Callable | None = None, clock: Callable = time.time) -> Callable[[dict], bool]:
    known: dict[str, list[dict]] = {}

    def admits(task: dict) -> bool:
        url = task.get("pr_url") or ""
        repo = repository(url)
        if task.get("state") != "open" or not repo:
            return True
        if repo not in known:
            try:
                known[repo] = (reader or read)(repo)
            except (ReadError, *ERRORS) as exc:
                print(f"fleet red hold skipped, listing open pull requests failed: {exc}", file=sys.stderr)
                known[repo] = []
        return not held(known[repo], url, int(clock() * 1000))

    return admits


def notice_pass(slug, store, ledger, rows, now_ms, reader: Callable | None = None) -> list[str]:
    unclaimed = [
        task["id"]
        for task in rows.values()
        if task.get("state") == "open" and task.get("pr_url") and not task.get("claimed_by")
    ]
    waiting = set(unclaimed) - store.claimed(slug, unclaimed)
    cleared = [store.key(slug, f"fleet-red:{task['id']}") for task in rows.values() if task["id"] not in waiting]
    listings: dict[str, list[dict] | None] = {}
    actions = []
    for task_id in [task_id for task_id in unclaimed if task_id in waiting]:
        url, key = rows[task_id]["pr_url"], store.key(slug, f"fleet-red:{task_id}")
        repo = repository(url)
        if repo not in listings:
            try:
                listings[repo] = (reader or read)(repo) if repo else []
            except (ReadError, *ERRORS):
                listings[repo] = None
        pulls = listings[repo]
        if pulls is None:
            continue
        if not held(pulls, url, now_ms):
            cleared.append(key)
            continue
        if store.redis.set(key, url, nx=True):
            checks = ", ".join(sorted(own(pulls, url)))
            ledger.notice(slug, task_id, NOTICE.format(url=url, checks=checks))
            actions.append(f"task {task_id} held unclaimed, {url} is red only from {checks}")
    if cleared:
        store.redis.delete(*cleared)
    return actions
