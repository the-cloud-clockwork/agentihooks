"""Hold a task's claim until its repository carries a release newer than the tag it names."""

import re
import subprocess
from collections.abc import Callable

FIELD = "after_release"
SOURCE = "release"
ALERT = "The release tag lookup for {repo} failed: {why}. Tasks {tasks} stay held for a newer release until it works."
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def version(tag: str) -> tuple[int, ...] | None:
    match = VERSION_RE.fullmatch(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def newer(tags: list[str], floor: str) -> bool:
    base = version(floor)
    return base is not None and any((found := version(tag)) is not None and found > base for tag in tags)


def tags(repo: str) -> list[str]:
    return lookup(repo)[0]


def lookup(repo: str) -> tuple[list[str], str]:
    """The origin's release tags, and why the lookup failed or ''."""
    try:
        found = subprocess.run(
            ["git", "-C", repo, "ls-remote", "--tags", "--refs", "origin"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc) or type(exc).__name__
    if found.returncode:
        return [], f"git ls-remote exited {found.returncode}"
    return [ref.removeprefix("refs/tags/") for _, ref in (line.split("\t") for line in found.stdout.splitlines())], ""


def alert_pass(slug: str, config: object, ledger: object, doc: dict) -> list[str]:
    held = [t["id"] for t in doc.get("tasks") or [] if t.get(FIELD) and t.get("state") != "done"]
    live = any(a.get("source") == SOURCE and a.get("state") != "done" for a in doc.get("alerts") or [])
    if not held or live:
        return []
    why = lookup(config.repo)[1]
    if not why:
        return []
    ledger.raise_alert(slug, SOURCE, ALERT.format(repo=config.repo, why=why, tasks=", ".join(held)))
    return [f"raised the release alert: the tag lookup for {config.repo} failed"]


def checker(repo: str) -> Callable[[dict], bool]:
    known: list[str] | None = None

    def released(task: dict) -> bool:
        nonlocal known
        floor = task.get(FIELD)
        if not floor:
            return True
        if known is None:
            known = tags(repo)
        return newer(known, floor)

    return released
