import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable

from hooks.lifecycle.act import ActionError, Journal, apply
from hooks.lifecycle.gitstate import inspect
from hooks.lifecycle.lease import read_lease
from hooks.lifecycle.liveness import BOOT_GRACE, Snapshot
from hooks.lifecycle.locks import removing
from hooks.lifecycle.model import Finding, Root
from hooks.lifecycle.worktrees import _held, discover, needs_rescue, registered, scratch_repository

GB = 1 << 30
FRESH = 60
EXTRA_FILESYSTEMS = ("/mnt/c",)


def free_gb(path: Path) -> int:
    while not path.is_dir():
        path = path.parent
    targets = [path, *(Path(extra) for extra in EXTRA_FILESYSTEMS if Path(extra).is_dir())]
    return min(shutil.disk_usage(target).free for target in targets) // GB


def _candidates(root: Root, snap: Snapshot) -> list[tuple[float, Path, Path, Finding]]:
    found = []
    for path, _root in discover([root]):
        state = inspect(path, snap.now)
        if state is None or snap.now - state.last_touch < FRESH:
            continue
        lease = read_lease(path, "worktree")
        if _held(state, lease, replace(snap, uptime=BOOT_GRACE)):
            continue
        rescue = needs_rescue(state, lease)
        disposable = rescue and scratch_repository(state, root)
        action = "snapshot" if rescue else "remove"
        item = Finding(str(path), root.id, "worktree", action, "evicted", disposable=disposable)
        found.append((state.last_touch, path, Path(state.primary).resolve(), item))
    return sorted(found)


def make_room(
    repo: Path,
    root: Root,
    floor_gb: int,
    cap: int,
    home: Path,
    snap: Snapshot,
    free: Callable[[Path], int] | None = None,
) -> list[str]:
    free, repo = free or free_gb, repo.resolve()
    journal, lines = Journal(home / "gc-journal.json"), []
    for _touch, path, primary, item in _candidates(root, snap):
        short = free(Path(root.path)) < floor_gb
        if not short and len(registered(repo)) - 1 < cap:
            break
        if not short and primary != repo:
            continue
        with removing(home, str(path)):
            try:
                outcome = apply(item, journal)
            except (ActionError, OSError, subprocess.TimeoutExpired) as error:
                lines.append(f"kept {path}: {error}")
                continue
        lines.append(f"evicted {path}: {outcome}")
    return lines
