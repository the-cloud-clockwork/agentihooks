"""Hold a task's claim until its repository carries a release newer than the tag it names."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.swarm import timing
from scripts.tick.deadline import Deadline

if TYPE_CHECKING:
    from scripts.swarm.store import SwarmConfig

FIELD = "after_release"
SOURCE = "release"
ALERT = "The release tag lookup for {repo} failed: {why}. Tasks {tasks} stay held for a newer release until it works."
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
HOST = "github.com"
SECONDS = 15
FAILURES = (OSError, subprocess.SubprocessError, TimeoutError, ValueError, KeyError, TypeError)


def version(tag: str) -> tuple[int, ...] | None:
    """The three release numbers of a stable tag, None for any other tag."""
    match = VERSION_RE.fullmatch(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def newer(tags: list[str], floor: str) -> bool:
    """Whether any stable tag is newer than the floor tag."""
    base = version(floor)
    return base is not None and any((found := version(tag)) is not None and found > base for tag in tags)


def repository(config: SwarmConfig) -> str:
    """The swarm's GitHub owner and name: its stored repository origin, else the origin its checkout's git config
    names, empty when neither is on GitHub."""
    from scripts.hive.daemon import origin

    host, _, name = (config.repo_origin or origin(Path(config.repo))).partition("/")
    return name if host == HOST else ""


def tags(config: SwarmConfig) -> list[str]:
    """The repository's tag names, empty when the lookup failed."""
    return lookup(config)[0]


def lookup(config: SwarmConfig) -> tuple[list[str], str]:
    """The repository's tags read through the GitHub API, a failed read retried once, and why it failed or ''."""
    name = repository(config)
    if not name:
        return [], f"{config.repo} names no GitHub repository"
    found, why = _attempt(name)
    return (found, why) if not why else _attempt(name)


def _attempt(name: str) -> tuple[list[str], str]:
    """One bounded API read of the repository's tags, and why it failed or ''."""
    from scripts.tick.adapters.github import GitHubSourceControl

    try:
        found = GitHubSourceControl(name, Path.cwd(), "dev").tags(deadline=Deadline.after(SECONDS))
    except FAILURES as exc:
        return [], str(exc) or type(exc).__name__
    return [tag.name for tag in found], ""


def alert_pass(slug: str, config: SwarmConfig, ledger: Any, doc: dict[str, Any]) -> list[str]:
    """Raise one release alert when tasks wait on a release and the tag lookup fails."""
    held = [t["id"] for t in doc.get("tasks") or [] if t.get(FIELD) and t.get("state") != "done"]
    live = any(a.get("source") == SOURCE and a.get("state") != "done" for a in doc.get("alerts") or [])
    if not held or live:
        return []
    why = lookup(config)[1]
    if not why:
        return []
    ledger.raise_alert(slug, SOURCE, ALERT.format(repo=config.repo, why=why, tasks=", ".join(held)))
    return [f"raised the release alert: the tag lookup for {config.repo} failed"]


def checker(config: SwarmConfig) -> Callable[[dict[str, Any]], bool]:
    """A check that a task's release floor has a newer tag, reading the tags at most once."""
    known: list[str] | None = None

    def released(task: dict[str, Any]) -> bool:
        nonlocal known
        floor = task.get(FIELD)
        if not floor:
            return True
        if known is None:
            known = timing.timed(tags, config)
        return newer(known, floor)

    return released
