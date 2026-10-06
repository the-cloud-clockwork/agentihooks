import os
from pathlib import Path

from hooks.lifecycle.gitstate import TreeState, git, inspect
from hooks.lifecycle.lease import admin_dir, read_lease
from hooks.lifecycle.liveness import Snapshot, in_boot_grace, lease_alive, path_in_use
from hooks.lifecycle.model import Finding, Lease, Root

PROTECTED = {"dev", "main", "master"}
CLEAN_IDLE = 2 * 3600
DIRTY_IDLE = 24 * 3600
NESTED_DEPTH = 8


def _is_worktree(path: Path) -> bool:
    try:
        return (path / ".git").read_bytes().startswith(b"gitdir:")
    except OSError:
        return False


def nested_worktrees(top: Path, depth: int = NESTED_DEPTH) -> list[Path]:
    found, stack = [], [(top, 0)]
    while stack:
        current, level = stack.pop()
        try:
            entries = [entry for entry in os.scandir(current) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in entries:
            child = Path(entry.path)
            if _is_worktree(child):
                found.append(child)
            elif level + 1 < depth and entry.name != ".git":
                stack.append((child, level + 1))
    return sorted(found)


def registered(primary: Path) -> list[Path]:
    result = git(primary, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return []
    return [Path(line[len("worktree ") :]) for line in result.stdout.splitlines() if line.startswith("worktree ")]


def _managing_root(path: Path, roots: list[Root]) -> Root | None:
    for root in roots:
        if root.kind in ("worktrees", "scratch") and path.is_relative_to(root.path):
            return root
    return None


def discover(roots: list[Root]) -> list[tuple[Path, Root]]:
    found: dict[Path, Root] = {}
    for root in roots:
        base = Path(root.path)
        if root.kind == "worktrees":
            candidates = sorted(base.glob("*/*")) + sorted(base.glob("*/_tmp/*"))
            found.update({path: root for path in candidates if _is_worktree(path)})
        elif root.kind == "scratch":
            found.update({path: root for path in nested_worktrees(base)})
    primaries = {admin.parent.parent.parent for path in found if (admin := admin_dir(path))}
    for primary in sorted(primaries):
        for path in registered(primary):
            root = _managing_root(path, roots)
            if path != primary and path not in found and root and _is_worktree(path):
                found[path] = root
    return sorted(found.items())


def _held(state: TreeState, lease: Lease | None, snap: Snapshot) -> str:
    if state.path == state.primary:
        return "primary checkout"
    if state.branch in PROTECTED:
        return f"protected branch {state.branch}"
    if state.locked and lease is None:
        return "git-locked"
    if state.busy:
        return f"git {state.busy} in progress"
    if path_in_use(state.path, snap):
        return "a process works inside"
    if lease_alive(lease, snap):
        return "owner session alive"
    if in_boot_grace(snap):
        return "boot grace"
    return ""


def _idle_verdict(state: TreeState, lease: Lease | None, idle: float) -> tuple[str, str]:
    if lease and lease.kind == "ephemeral":
        return ("remove", "throwaway checkout, owner gone") if idle >= CLEAN_IDLE else ("keep", "throwaway, recent")
    if not state.dirty and not state.unpushed:
        return ("remove", "clean and on a remote") if idle >= CLEAN_IDLE else ("keep", "clean, recent")
    if idle >= DIRTY_IDLE:
        return "snapshot", "unique work, owner gone: push to wip/ then remove"
    return "keep", "unique work, recent"


def classify(path: Path, root: Root, snap: Snapshot) -> Finding:
    state = inspect(path, snap.now)
    if state is None:
        return Finding(str(path), root.id, "worktree", "skip", "unreadable worktree")
    lease = read_lease(path, "worktree")
    held = _held(state, lease, snap)
    action, reason = ("keep", held) if held else _idle_verdict(state, lease, snap.now - state.last_touch)
    if state.busy and held == f"git {state.busy} in progress":
        action = "skip"
    return Finding(str(path), root.id, "worktree", action, reason, state.last_touch)
