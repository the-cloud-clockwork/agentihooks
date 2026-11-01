"""Which held tasks have a pull request waiting on checks or operator approval."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.swarm import stack
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import CheckState

if TYPE_CHECKING:
    from redis import Redis

CACHE_SECONDS = 60
CHECKS_SECONDS = 20
SKIPPED = ("skipped", "neutral")

Read = Callable[[str], tuple[CheckState, ...]]


def read(pr_url: str) -> tuple[CheckState, ...]:
    """Read the checks on a pull request's head through its source control port."""
    provider, reference = stack.source_control(pr_url)
    return provider.checks(reference, deadline=Deadline.after(CHECKS_SECONDS))


def state(check: CheckState) -> str:
    """Name a check pass, skipping, pending or fail."""
    if check.status != "completed":
        return "pending"
    if check.conclusion == "success":
        return "pass"
    return "skipping" if check.conclusion in SKIPPED else "fail"


def _states(pr_url: str, read: Read) -> list[str]:
    try:
        return [state(check) for check in read(pr_url)]
    except stack.PORT_ERRORS:
        return []


def pending(pr_url: str, read: Read = read, approval: bool = False) -> bool:
    """Whether a pull request still waits on a check, or with approval on the operator once every check passed."""
    states = _states(pr_url, read)
    return "pending" in states or (approval and bool(states) and all(s == "pass" for s in states))


def passing(pr_url: str, read: Read = read) -> bool:
    """Whether every check on a pull request passed or was skipped, with at least one passed."""
    states = _states(pr_url, read)
    return "pass" in states and all(s in ("pass", "skipping") for s in states)


def _memo(redis, key, compute):
    hit = redis.get(key)
    if hit is None:
        hit = "1" if compute() else "0"
        redis.set(key, hit, ex=CACHE_SECONDS)
    return hit == "1"


def cached(redis: Redis, prefix: str, read: Read = read, approval: bool = False) -> Callable[[str], bool]:
    """Probe whether a pull request waits on checks, answering from a per minute cache."""

    def probe(pr_url: str) -> bool:
        key = f"{prefix}:approval:{pr_url}" if approval else f"{prefix}:{pr_url}"
        return _memo(redis, key, lambda: pending(pr_url, read, approval=approval))

    return probe


def cached_green(redis: Redis, prefix: str, read: Read = read) -> Callable[[str], bool]:
    """Probe whether a pull request's checks passed, answering from a per minute cache."""
    return lambda pr_url: _memo(redis, f"{prefix}:green:{pr_url}", lambda: passing(pr_url, read))


def from_view(view: Callable, approval: bool = False) -> tuple[Callable, Callable]:
    def pending(pr_url):
        current = view(pr_url)
        return current is not None and (current.checks_pending or approval and current.checks_passed)

    def passing(pr_url):
        current = view(pr_url)
        return current is not None and current.checks_passed

    return pending, passing


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
