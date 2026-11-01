"""Which held tasks have a pull request waiting on checks or operator approval."""

import subprocess
from collections.abc import Callable
from typing import Any

from scripts.tick.ports.source_control import CheckState

CACHE_SECONDS = 60
SKIPPED = ("skipped", "neutral")

Read = Callable[[str], tuple[CheckState, ...]]


def read_checks(pr_url: str) -> tuple[CheckState, ...]:
    """Read the check runs on a pull request's head through the source control port."""
    from scripts.swarm import stack
    from scripts.tick.deadline import Deadline

    provider, reference = stack.source_control(pr_url)
    return provider.checks(reference, deadline=Deadline.after(stack.PORT_SECONDS))


def _state(check: CheckState) -> str:
    """Name one check run as pending, pass, skipping or fail."""
    if check.status != "completed":
        return "pending"
    if check.conclusion == "success":
        return "pass"
    return "skipping" if check.conclusion in SKIPPED else "fail"


def _states(pr_url: str, read: Read) -> list[str]:
    """Return the state of every check on the pull request, none when they cannot be read."""
    try:
        return [_state(check) for check in read(pr_url)]
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, KeyError):
        return []


def pending(pr_url: str, read: Read = read_checks, approval: bool = False) -> bool:
    """Whether a check is still running, or with approval, every check passed."""
    states = _states(pr_url, read)
    return "pending" in states or (approval and bool(states) and all(state == "pass" for state in states))


def passing(pr_url: str, read: Read = read_checks) -> bool:
    """Whether at least one check passed and every other passed or skipped."""
    states = _states(pr_url, read)
    return "pass" in states and all(state in ("pass", "skipping") for state in states)


def _memo(redis, key, compute):
    hit = redis.get(key)
    if hit is None:
        hit = "1" if compute() else "0"
        redis.set(key, hit, ex=CACHE_SECONDS)
    return hit == "1"


def cached(redis: Any, prefix: str, read: Read = read_checks, approval: bool = False) -> Callable[[str], bool]:
    def probe(pr_url: str) -> bool:
        key = f"{prefix}:approval:{pr_url}" if approval else f"{prefix}:{pr_url}"
        return _memo(redis, key, lambda: pending(pr_url, read, approval=approval))

    return probe


def cached_green(redis: Any, prefix: str, read: Read = read_checks) -> Callable[[str], bool]:
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
