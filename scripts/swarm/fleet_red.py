"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import json
import subprocess
import sys
import time
from collections.abc import Callable

from scripts.swarm.ledger_events import RED, iso_ms

MIN_OTHERS = 3
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


def read(repo: str, run: Callable = subprocess.run) -> list[dict]:
    found = run(
        ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "url,statusCheckRollup"],
        cwd=repo,
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


def checker(repo: str, reader: Callable | None = None, clock: Callable = time.time) -> Callable[[dict], bool]:
    known: list[dict] | None = None

    def admits(task: dict) -> bool:
        nonlocal known
        url = task.get("pr_url")
        if task.get("state") != "open" or not url:
            return True
        if known is None:
            try:
                known = (reader or read)(repo)
            except (ReadError, *ERRORS) as exc:
                print(f"fleet red hold skipped, listing open pull requests failed: {exc}", file=sys.stderr)
                known = []
        return not held(known, url, int(clock() * 1000))

    return admits


def notice_pass(slug, config, store, ledger, rows, now_ms, reader: Callable | None = None) -> list[str]:
    waiting = []
    for task in rows.values():
        key = store.key(slug, f"fleet-red:{task['id']}")
        claimed = task.get("claimed_by") or store.claimant(slug, task["id"])
        if task.get("state") == "open" and task.get("pr_url") and not claimed:
            waiting.append((task, key))
        else:
            store.redis.delete(key)
    if not waiting:
        return []
    try:
        pulls = (reader or read)(config.repo)
    except (ReadError, *ERRORS):
        return []
    actions = []
    for task, key in waiting:
        task_id, url = task["id"], task["pr_url"]
        if not held(pulls, url, now_ms):
            store.redis.delete(key)
            continue
        if store.redis.set(key, url, nx=True):
            checks = ", ".join(sorted(own(pulls, url)))
            ledger.notice(slug, task_id, NOTICE.format(url=url, checks=checks))
            actions.append(f"task {task_id} held unclaimed, {url} is red only from {checks}")
    return actions
