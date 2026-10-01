import fcntl
import json
from dataclasses import asdict, replace
from pathlib import Path

from hooks.lifecycle.config import load_roots
from hooks.lifecycle.files import classify_files
from hooks.lifecycle.liveness import Snapshot, take_snapshot
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
    return findings


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
    roots: list[Root] | None = None, snap: Snapshot | None = None, home: Path | None = None, scope: str = ""
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
        report = summarize(findings, snap)
        (home / "gc-last.json").write_text(json.dumps(report), encoding="utf-8")
        return report
