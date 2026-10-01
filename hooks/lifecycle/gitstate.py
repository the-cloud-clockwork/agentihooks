import subprocess
from dataclasses import dataclass
from pathlib import Path

from hooks.lifecycle.lease import admin_dir

BUSY_MARKERS = (
    "MERGE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "index.lock",
)


@dataclass(frozen=True)
class TreeState:
    path: str
    admin: str
    primary: str
    branch: str
    dirty: tuple[str, ...]
    unpushed: bool
    busy: str
    locked: bool
    last_touch: float


def git(path: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout
    )


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


def _dirty(path: Path) -> tuple[str, ...] | None:
    result = git(path, "status", "--porcelain", "--untracked-files=normal")
    if result.returncode != 0:
        return None
    return tuple(line[3:].split(" -> ")[-1].strip('"') for line in result.stdout.splitlines() if line)


def _unpushed(path: Path) -> bool | None:
    result = git(path, "rev-list", "-n1", "HEAD", "--not", "--remotes")
    return None if result.returncode != 0 else bool(result.stdout.strip())


def inspect(path: Path, now: float) -> TreeState | None:
    admin = admin_dir(path)
    if admin is None or not admin.is_dir():
        return None
    touched = [_mtime(admin / name) for name in ("index", "HEAD", "logs/HEAD")]
    try:
        dirty, unpushed = _dirty(path), _unpushed(path)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if dirty is None or unpushed is None:
        return None
    branch = git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    touched += [_mtime(path / item) for item in dirty]
    return TreeState(
        path=str(path),
        admin=str(admin),
        primary=str(admin.parent.parent.parent),
        branch=branch,
        dirty=dirty,
        unpushed=unpushed,
        busy=next((name for name in BUSY_MARKERS if (admin / name).exists()), ""),
        locked=(admin / "locked").exists(),
        last_touch=min(max(touched, default=0.0), now),
    )
