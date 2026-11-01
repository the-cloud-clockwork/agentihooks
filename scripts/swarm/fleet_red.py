"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from scripts.swarm import panic
from scripts.swarm.ledger_events import RED, iso_ms
from scripts.swarm.stack import PORT_ERRORS
from scripts.swarm.store import RedisStore
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import OpenChange, RollupCheck

MIN_OTHERS = 3
PULL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/\d+")
WINDOW_MS = 6 * 3_600_000
SECONDS = 60
TTL = 600
ERRORS = PORT_ERRORS
NOTICE = (
    "The swarm holds this task unclaimed: its pull request {url} is red only from {checks}, which also fail on "
    "most other open pull requests. It is claimed again once those checks stop failing elsewhere."
)

Pulls = Sequence[OpenChange]
Reader = Callable[[str], Pulls]


def failing(pull: OpenChange) -> set[str]:
    """Name the checks on an open pull request whose result is red."""
    return {check.name for check in pull.checks if check.result in RED}


def own(pulls: Pulls, url: str) -> set[str]:
    """Name the red checks on the listed pull request at the url, none when it is not listed."""
    return next((failing(p) for p in pulls if p.url == url), set())


def recent(pull: OpenChange, now_ms: int) -> bool:
    """Say whether any check on the pull request finished inside the window."""
    return any(iso_ms(check.at) >= now_ms - WINDOW_MS for check in pull.checks if check.at)


def repository(url: str) -> str:
    found = PULL.match(url)
    return found.group(1) if found else ""


def read(repo: str, ports: Callable[[str], SourceControl] = panic.port) -> Pulls:
    """List the repository's open pull requests with their check rollups through its source control port."""
    return ports(repo).open_changes(deadline=Deadline.after(SECONDS))


def cached(store: RedisStore, slug: str, repo: str, reader: Reader | None = None) -> Pulls | None:
    """Return the repository's open pull requests saved in the last ten minutes, else list and save them, a failed listing as none."""
    key = store.key(slug, f"fleet-red-pulls:{repo}")
    saved = store.redis.get(key)
    if saved is not None:
        rows = json.loads(saved)
        if rows is None:
            return None
        return tuple(OpenChange(url, tuple(RollupCheck(*check) for check in checks)) for url, checks in rows)
    try:
        pulls: Pulls | None = tuple((reader or read)(repo))
    except ERRORS:
        pulls = None
    listing = None if pulls is None else [[p.url, [[c.name, c.result, c.at] for c in p.checks]] for p in pulls]
    store.redis.set(key, json.dumps(listing), ex=TTL)
    return pulls


def shared(pulls: Pulls, url: str, now_ms: int) -> set[str]:
    others = [failing(p) for p in pulls if p.url != url and recent(p, now_ms)]
    if len(others) < MIN_OTHERS:
        return set()
    return {name for name in own(pulls, url) if sum(name in other for other in others) * 2 > len(others)}


def held(pulls: Pulls, url: str, now_ms: int) -> bool:
    mine = own(pulls, url)
    return bool(mine) and mine <= shared(pulls, url, now_ms)


def checker(reader: Reader | None = None, clock: Callable[[], float] = time.time) -> Callable[[dict[str, Any]], bool]:
    """Return the claim filter that holds an open task whose pull request is red only from fleet wide checks."""
    known: dict[str, Pulls] = {}

    def admits(task: dict[str, Any]) -> bool:
        """Admit the task unless its pull request is red only from checks failing on most other open ones."""
        url = task.get("pr_url") or ""
        repo = repository(url)
        if task.get("state") != "open" or not repo:
            return True
        if repo not in known:
            try:
                known[repo] = (reader or read)(repo)
            except ERRORS as exc:
                print(f"fleet red hold skipped, listing open pull requests failed: {exc}", file=sys.stderr)
                known[repo] = []
        return not held(known[repo], url, int(clock() * 1000))

    return admits


def notice_pass(
    slug: str,
    store: RedisStore,
    ledger: Any,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
    reader: Reader | None = None,
) -> list[str]:
    """Post one notice per unclaimed open task newly held by fleet red, and forget holds that lifted."""
    unclaimed = [
        task["id"]
        for task in rows.values()
        if task.get("state") == "open" and task.get("pr_url") and not task.get("claimed_by")
    ]
    waiting = set(unclaimed) - store.claimed(slug, unclaimed)
    cleared = [store.key(slug, f"fleet-red:{task['id']}") for task in rows.values() if task["id"] not in waiting]
    listings: dict[str, Pulls | None] = {}
    fetch: Callable[[str], Pulls | None] = reader or partial(cached, store, slug)
    actions = []
    for task_id in [task_id for task_id in unclaimed if task_id in waiting]:
        url, key = rows[task_id]["pr_url"], store.key(slug, f"fleet-red:{task_id}")
        repo = repository(url)
        if repo not in listings:
            try:
                listings[repo] = fetch(repo) if repo else []
            except ERRORS:
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
