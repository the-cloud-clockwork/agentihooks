import fcntl
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from hooks.lifecycle.act import ActionError, Journal, apply, finish_pending
from hooks.lifecycle.config import load_roots
from hooks.lifecycle.files import classify_files, classify_traces
from hooks.lifecycle.lease import read_lease
from hooks.lifecycle.liveness import Snapshot, lease_alive, path_in_use, take_snapshot
from hooks.lifecycle.locks import removing
from hooks.lifecycle.model import ACTIONABLE, Finding, Root
from hooks.lifecycle.scratch import classify_scratch, walk_stats
from hooks.lifecycle.state import confirm
from hooks.lifecycle.worktrees import classify, discover


def state_home() -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return Path(AGENTIHOOKS_HOME)


def collect(roots: list[Root], snap: Snapshot) -> list[Finding]:
    trees = [classify(path, root, snap) for path, root in discover(roots)]
    trees = [
        replace(item, size=walk_stats(Path(item.path))[1]) if item.action in ACTIONABLE else item for item in trees
    ]
    held = {item.path for item in trees if item.action != "remove"}
    findings = list(trees)
    for root in roots:
        if root.kind == "scratch":
            findings += classify_scratch(root, snap, held)
        elif root.kind in ("ttl", "archive"):
            findings += classify_files(root, snap)
        elif root.kind == "traces":
            findings += classify_traces(root, snap)
    return findings


def _still_safe(item: Finding, roots: dict[str, Root], fresh: Snapshot) -> bool:
    if item.category == "worktree":
        return classify(Path(item.path), roots[item.root], fresh).action == item.action
    path = Path(item.path)
    if path_in_use(item.path, fresh) or (path / ".keep").exists():
        return False
    if item.category == "trace":
        return path.stem not in fresh.sessions.values()
    return not (item.category == "scratch" and lease_alive(read_lease(path, "scratch"), fresh))


def enforce(findings: list[Finding], roots: list[Root], home: Path, fresh: Callable[[], Snapshot]) -> list[Finding]:
    journal = Journal(home / "gc-journal.json")
    finish_pending(journal)
    by_id, view, result = {root.id: root for root in roots}, fresh(), []
    for item in findings:
        if not (item.due and item.action in ACTIONABLE):
            result.append(item)
            continue
        with removing(home, item.path):
            if not _still_safe(item, by_id, view):
                result.append(replace(item, outcome="skipped: no longer safe"))
                continue
            try:
                outcome = apply(item, journal)
            except (ActionError, OSError, subprocess.TimeoutExpired) as error:
                outcome = f"failed: {error}"
        result.append(replace(item, outcome=outcome))
    return result


def summarize(findings: list[Finding], snap: Snapshot) -> dict:
    totals: dict[str, dict] = {}
    for item in findings:
        bucket = totals.setdefault(item.action, {"count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += item.size
    return {
        "generated_at": snap.now,
        "boot_id": snap.boot_id,
        "uptime": snap.uptime,
        "totals": totals,
        "findings": [asdict(item) for item in findings],
    }


def _within(path: str, scope: str) -> bool:
    scope = scope.rstrip("/")
    return path == scope or path.startswith(scope + "/")


def sweep(
    roots: list[Root] | None = None,
    snap: Snapshot | None = None,
    home: Path | None = None,
    scope: str = "",
    act: bool = False,
    fresh: Callable[[], Snapshot] = take_snapshot,
) -> dict:
    home = home or state_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "gc.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "another sweep is running"}
        snap = snap or take_snapshot()
        roots = load_roots() if roots is None else roots
        findings = confirm(collect(roots, snap), snap, home / "gc-state.json")
        if scope:
            findings = [item for item in findings if _within(item.path, scope)]
        if act:
            findings = enforce(findings, roots, home, fresh)
        report = summarize(findings, snap)
        (home / "gc-last.json").write_text(json.dumps(report), encoding="utf-8")
        return report
