import os
from pathlib import Path

from hooks.lifecycle.act import ActionError, remove_path, remove_worktree
from hooks.lifecycle.gitstate import inspect
from hooks.lifecycle.lease import read_lease
from hooks.lifecycle.liveness import Snapshot, holder_alive, owner_holder
from hooks.lifecycle.model import Holder, Root
from hooks.lifecycle.worktrees import nested_worktrees


def _ancestors(snap: Snapshot, pid: int) -> set[int]:
    seen: set[int] = set()
    while pid in snap.table and pid not in seen:
        seen.add(pid)
        pid = snap.table[pid].ppid
    return seen


def _same(a: Holder, b: Holder) -> bool:
    return (a.session_id and a.session_id == b.session_id) or (a.pid, a.start_time, a.boot_id) == (
        b.pid,
        b.start_time,
        b.boot_id,
    )


def _foreign_process(path: Path, snap: Snapshot, mine: set[int], proc: Path) -> str:
    prefix = str(path) + "/"
    for pid, process in snap.table.items():
        if pid in mine:
            continue
        try:
            cwd = os.readlink(proc / str(pid) / "cwd")
        except OSError:
            continue
        if cwd == str(path) or cwd.startswith(prefix):
            return f"process {pid} ({process.comm}) works inside"
    return ""


def _nested_work(path: Path, snap: Snapshot) -> str:
    for tree in nested_worktrees(path):
        state = inspect(tree, snap.now)
        lease = read_lease(tree, "worktree")
        if state is None:
            return f"nested worktree {tree} is unreadable"
        if (state.dirty or state.unpushed) and not (lease and lease.kind == "ephemeral"):
            return f"nested worktree {tree} holds uncommitted or unpushed work; ship it or run wt.sh done"
    return ""


def refusal(path: Path, roots: list[Root], snap: Snapshot, caller: int, proc: Path = Path("/proc")) -> str:
    bases = [Path(root.path) for root in roots if root.kind == "scratch"]
    if not any(path.is_relative_to(base) and len(path.relative_to(base).parts) >= 2 for base in bases):
        return "not a task dir under a scratch root (<root>/<repo>/<task>)"
    if not path.is_dir():
        return "no such directory"
    reason = _foreign_process(path, snap, _ancestors(snap, caller), proc)
    if reason:
        return reason
    owner, lease = owner_holder(snap, caller), read_lease(path, "scratch")
    for holder in lease.holders if lease else ():
        if holder_alive(holder, snap) and not (owner and _same(holder, owner)):
            return "held by another live session"
    return _nested_work(path, snap)


def remove_scratch(path: Path, roots: list[Root], snap: Snapshot, caller: int) -> None:
    reason = refusal(path, roots, snap, caller)
    if reason:
        raise ActionError(reason)
    for tree in nested_worktrees(path):
        remove_worktree(tree)
    remove_path(path)
