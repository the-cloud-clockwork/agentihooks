from pathlib import Path

from hooks.lifecycle.liveness import Snapshot, in_boot_grace, path_in_use
from hooks.lifecycle.model import Finding, Root
from hooks.lifecycle.scratch import DAY, walk_stats

ARCHIVE_DIR = "archive"


def _candidates(root: Root) -> list[Path]:
    base = Path(root.path)
    if not base.is_dir():
        return []
    if root.include:
        found = {path for pattern in root.include for path in base.glob(pattern) if path.is_file()}
    else:
        found = set(base.iterdir())
    return sorted(path for path in found if not path.name.startswith(".") and path.name != ARCHIVE_DIR)


def classify_files(root: Root, snap: Snapshot) -> list[Finding]:
    if in_boot_grace(snap):
        return []
    action = "remove" if root.kind == "ttl" else "archive"
    findings = []
    for path in _candidates(root):
        if path_in_use(str(path), snap) or (path.is_dir() and (path / ".keep").exists()):
            continue
        newest, size = walk_stats(path)
        newest = min(newest, snap.now)
        if snap.now - newest >= root.idle_days * DAY:
            findings.append(
                Finding(str(path), root.id, "file", action, f"idle over {root.idle_days:g} days", newest, size)
            )
    return findings
