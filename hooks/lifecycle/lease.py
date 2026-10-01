import fcntl
import json
import os
from dataclasses import asdict
from pathlib import Path

from hooks.lifecycle.model import Holder, Lease

WORKTREE_LEASE = "agentihooks-lease.json"
SCRATCH_LEASE = ".lease.json"
MAX_HOLDERS = 8


def admin_dir(worktree: Path) -> Path | None:
    try:
        text = (worktree / ".git").read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    return target if target.is_absolute() else (worktree / target).resolve()


def lease_path(path: Path, kind: str) -> Path | None:
    if kind == "scratch":
        return path / SCRATCH_LEASE
    admin = admin_dir(path)
    return admin / WORKTREE_LEASE if admin else None


def _decode(text: str) -> Lease | None:
    try:
        data = json.loads(text)
        holders = tuple(Holder(**item) for item in data.get("holders", []))
        return Lease(str(data["kind"]), float(data["created_at"]), holders)
    except (ValueError, KeyError, TypeError):
        return None


def read_lease(path: Path, kind: str) -> Lease | None:
    file = lease_path(path, kind)
    try:
        return _decode(file.read_text(encoding="utf-8")) if file else None
    except OSError:
        return None


def _key(holder: Holder) -> tuple:
    return (holder.session_id,) if holder.session_id else (holder.pid, holder.start_time, holder.boot_id)


def add_holder(path: Path, kind: str, holder: Holder, now: float) -> Lease | None:
    file = lease_path(path, kind)
    if file is None:
        return None
    fd = os.open(file, os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        current = _decode(handle.read()) or Lease(kind, now)
        kept = [item for item in current.holders if _key(item) != _key(holder)]
        lease = Lease(current.kind, current.created_at, tuple([*kept, holder][-MAX_HOLDERS:]))
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(asdict(lease)))
    return lease
