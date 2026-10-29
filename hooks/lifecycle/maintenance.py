import subprocess
from pathlib import Path

from hooks.lifecycle.config import load_roots
from hooks.lifecycle.gitstate import git
from hooks.lifecycle.lease import admin_dir
from hooks.lifecycle.timer import _live_home
from hooks.lifecycle.worktrees import discover


def primaries(worktrees: list[Path]) -> list[Path]:
    return sorted(
        {
            admin.parent.parent.parent
            for path in worktrees
            if (admin := admin_dir(path)) and admin.parent.name == "worktrees"
        }
    )


def registered_repos() -> list[str]:
    try:
        result = git(Path.home(), "config", "--global", "--get-all", "maintenance.repo")
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [str(Path(line).resolve()) for line in result.stdout.splitlines() if line]


def objects(primary: Path) -> dict:
    result = git(primary, "count-objects", "-v")
    if result.returncode != 0:
        return {"loose": None, "packs": None}
    counts = dict(line.split(": ") for line in result.stdout.splitlines())
    return {"loose": int(counts["count"]), "packs": int(counts["packs"])}


def register(primary: Path) -> str:
    result = git(primary, "maintenance", "start" if _live_home() else "register")
    return "registered" if result.returncode == 0 else f"failed: {result.stderr.strip()}"


def _entry(primary: Path, registered: bool, act: bool) -> dict:
    try:
        outcome = register(primary) if act and not registered else ""
        counts = objects(primary)
    except (OSError, subprocess.TimeoutExpired) as error:
        outcome, counts = f"failed: {error}", {"loose": None, "packs": None}
    registered = registered or outcome == "registered"
    return {"path": str(primary), "registered": registered, "outcome": outcome, **counts}


def maintain(paths: list[Path], act: bool) -> list[dict]:
    known = set(registered_repos())
    return [_entry(primary, str(primary.resolve()) in known, act) for primary in paths]


def setup() -> list[str]:
    report = maintain(primaries([path for path, _root in discover(load_roots())]), act=True)
    return [
        f"[{'OK' if item['registered'] else '!!'}] git maintenance {item['outcome']} for {item['path']}"
        for item in report
        if item["outcome"]
    ]
