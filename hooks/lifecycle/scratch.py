import os
from pathlib import Path

from hooks.lifecycle.lease import read_lease
from hooks.lifecycle.liveness import Snapshot, in_boot_grace, lease_alive, path_in_use
from hooks.lifecycle.model import Finding, Root

DAY = 86400
GB = 1 << 30


def walk_stats(path: Path) -> tuple[float, int]:
    try:
        newest, size = path.lstat().st_mtime, 0
    except OSError:
        return 0.0, 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            newest, size = max(newest, info.st_mtime), size + info.st_blocks * 512
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return newest, size


def task_dirs(root: Path) -> list[Path]:
    found = []
    for group in sorted(root.iterdir()) if root.is_dir() else []:
        if group.is_dir() and not group.is_symlink():
            found += [task for task in sorted(group.iterdir()) if task.is_dir() and not task.is_symlink()]
    return found


def _held(path: Path, snap: Snapshot, held_trees: set[str]) -> str:
    prefix = str(path) + "/"
    if (path / ".keep").exists():
        return "pinned (.keep)"
    if any(tree.startswith(prefix) for tree in held_trees):
        return "holds a worktree with work"
    if path_in_use(str(path), snap):
        return "a process works inside"
    if lease_alive(read_lease(path, "scratch"), snap):
        return "owner session alive"
    if in_boot_grace(snap):
        return "boot grace"
    return ""


def _evict(findings: list[Finding], budget: int) -> list[Finding]:
    total = sum(item.size for item in findings if item.action != "remove")
    result = list(findings)
    for index in sorted(range(len(result)), key=lambda i: result[i].last_touch):
        if total <= budget:
            break
        item = result[index]
        if item.action == "keep" and item.reason == "recent":
            result[index] = Finding(
                item.path, item.root, item.category, "remove", "over budget, oldest first", item.last_touch, item.size
            )
            total -= item.size
    return result


def classify_scratch(root: Root, snap: Snapshot, held_trees: set[str]) -> list[Finding]:
    findings = []
    for path in task_dirs(Path(root.path)):
        newest, size = walk_stats(path)
        newest = min(newest, snap.now)
        held = _held(path, snap, held_trees)
        if held:
            action, reason = "keep", held
        elif snap.now - newest >= root.idle_days * DAY:
            action, reason = "remove", f"idle over {root.idle_days:g} days"
        else:
            action, reason = "keep", "recent"
        findings.append(Finding(str(path), root.id, "scratch", action, reason, newest, size))
    if root.budget_gb:
        findings = _evict(findings, int(root.budget_gb * GB))
    return findings
