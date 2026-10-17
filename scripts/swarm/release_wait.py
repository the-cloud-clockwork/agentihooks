"""Hold a task's claim until its repository carries a release newer than the tag it names."""

import re
import subprocess
from collections.abc import Callable

FIELD = "after_release"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def version(tag: str) -> tuple[int, ...] | None:
    match = VERSION_RE.fullmatch(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def newer(tags: list[str], floor: str) -> bool:
    base = version(floor)
    return base is not None and any((found := version(tag)) is not None and found > base for tag in tags)


def tags(repo: str) -> list[str]:
    try:
        found = subprocess.run(
            ["git", "-C", repo, "ls-remote", "--tags", "--refs", "origin"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if found.returncode:
        return []
    return [ref.removeprefix("refs/tags/") for _, ref in (line.split("\t") for line in found.stdout.splitlines())]


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
