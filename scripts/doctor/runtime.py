import os
import shutil
from collections.abc import Mapping
from heapq import nlargest
from pathlib import Path

from scripts.swarm.health.findings import Finding


def _allocated(directory):
    size = directory.lstat().st_blocks * 512
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in [*directories, *files]:
            path = Path(parent) / name
            try:
                size += path.lstat().st_blocks * 512
            except FileNotFoundError:
                pass
    return size


def _executions(root, now_ms):
    homes = root / "cx"
    if homes.is_symlink():
        return
    for directory in homes.glob("*"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            modified = directory.stat().st_mtime
            size = _allocated(directory)
        except FileNotFoundError:
            continue
        age = max(0, now_ms / 1000 - modified) / 60
        yield size, str(directory), age


def findings(now_ms: int, environ: Mapping[str, str]) -> list[Finding]:
    root = Path(environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if not root.exists():
        return []
    usage = shutil.disk_usage(root)
    share = usage.used / usage.total
    if share <= 0.8:
        return []
    folders = nlargest(3, _executions(root, now_ms))
    evidence = (
        f"runtime {root}: {usage.used} of {usage.total} bytes used",
        *(f"execution {path}: {size} allocated bytes, age {age:.1f} minutes" for size, path, age in folders),
    )
    return [
        Finding(
            "runtime storage", str(root), f"user runtime folder is {share:.1%} full", evidence, "over 80% full", share
        )
    ]
