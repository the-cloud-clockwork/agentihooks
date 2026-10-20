import json
import os
import socket
from collections.abc import Mapping
from pathlib import Path

from hooks.lifecycle.liveness import Snapshot
from hooks.lifecycle.scratch import walk_stats
from scripts.codex_session.supervisor import RUNTIME_SUBDIR, SOCKET_NAME, remove_home, runtime_root

# A launch copies its home before the server starts, so a new home has no owner for a moment.
SETTLE_S = 600
LOG = "gc-codex-homes.jsonl"


def roots(environ: Mapping[str, str]) -> list[Path]:
    legacy = Path(environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / RUNTIME_SUBDIR
    return [legacy, runtime_root(environ)]


def listening(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


def references(snap: Snapshot, proc: Path) -> list[str]:
    found = [*snap.cwds]
    for row in snap.table.values():
        found += row.argv
        try:
            links = sorted((proc / str(row.pid) / "fd").iterdir())
        except OSError:
            continue
        for link in links:
            try:
                found.append(os.readlink(link))
            except OSError:
                continue
    return [f"{item}/" for item in found]


def owned(directory: Path, refs: list[str]) -> bool:
    marker = f"{directory}/"
    return any(marker in ref for ref in refs) or listening(directory / SOCKET_NAME)


def homes(environ: Mapping[str, str], scope: str) -> list[Path]:
    found = []
    for root in roots(environ):
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for directory in entries:
            if directory.is_symlink() or not directory.is_dir():
                continue
            if not scope or directory.is_relative_to(scope):
                found.append(directory)
    return found


def sweep_codex_homes(
    snap: Snapshot,
    home: Path,
    scope: str = "",
    act: bool = False,
    proc: Path = Path("/proc"),
    environ: Mapping[str, str] = os.environ,
) -> list[dict]:
    refs = references(snap, proc)
    records = []
    for directory in homes(environ, scope):
        try:
            if owned(directory, refs):
                continue
            newest, size = walk_stats(directory)
            if not newest or snap.now - newest < SETTLE_S:
                continue
            info = {"path": str(directory), "bytes": size, "action": "would remove"}
            if act:
                remove_home(directory)
                if directory.exists():
                    raise OSError("home still present after removal")
                info["action"] = "removed"
        except (OSError, ValueError) as exc:
            info = {"path": str(directory), "action": "error", "error": str(exc)}
        if info["action"] == "removed":
            try:
                with (home / LOG).open("a") as log:
                    log.write(json.dumps(info) + "\n")
            except OSError as exc:
                info["log_error"] = str(exc)
        records.append(info)
    return records
