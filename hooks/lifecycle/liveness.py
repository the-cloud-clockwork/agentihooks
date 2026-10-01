import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from hooks.lifecycle.model import Holder, Lease
from hooks.proc import Process, _target, processes

BOOT_GRACE = 2 * 3600


@dataclass(frozen=True)
class Snapshot:
    boot_id: str
    uptime: float
    now: float
    table: dict[int, Process] = field(default_factory=dict)
    cwds: tuple[str, ...] = ()
    sessions: dict[int, str] = field(default_factory=dict)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cwds(table: dict[int, Process], proc: Path) -> tuple[str, ...]:
    found = set()
    for pid in table:
        try:
            found.add(os.readlink(proc / str(pid) / "cwd"))
        except OSError:
            continue
    return tuple(sorted(found))


def _live_sessions(table: dict[int, Process], sessions_dir: Path) -> dict[int, str]:
    live: dict[int, str] = {}
    try:
        files = list(sessions_dir.glob("*.json"))
    except OSError:
        return live
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid, start, session_id = int(data["pid"]), str(data.get("procStart", "")), str(data["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        process = table.get(pid)
        if start and process and str(process.start_time) == start:
            live[pid] = session_id
    return live


def take_snapshot(proc: Path = Path("/proc"), sessions_dir: Path | None = None) -> Snapshot:
    table = processes(proc)
    uptime = _read(proc / "uptime").split()
    return Snapshot(
        boot_id=_read(proc / "sys" / "kernel" / "random" / "boot_id"),
        uptime=float(uptime[0]) if uptime else 0.0,
        now=time.time(),
        table=table,
        cwds=_cwds(table, proc),
        sessions=_live_sessions(table, sessions_dir or Path.home() / ".claude" / "sessions"),
    )


def holder_alive(holder: Holder, snap: Snapshot) -> bool:
    if holder.session_id and holder.session_id in snap.sessions.values():
        return True
    process = snap.table.get(holder.pid)
    return bool(process) and holder.boot_id == snap.boot_id and process.start_time == holder.start_time


def lease_alive(lease: Lease | None, snap: Snapshot) -> bool:
    return bool(lease) and any(holder_alive(holder, snap) for holder in lease.holders)


def path_in_use(path: str, snap: Snapshot) -> bool:
    prefix = path.rstrip("/") + "/"
    return any(cwd == path or cwd.startswith(prefix) for cwd in snap.cwds)


def in_boot_grace(snap: Snapshot) -> bool:
    return snap.uptime < BOOT_GRACE


def owner_holder(snap: Snapshot, start_pid: int | None = None) -> Holder | None:
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(64):
        process = snap.table.get(pid)
        if process is None:
            return None
        if pid in snap.sessions:
            return Holder(snap.sessions[pid], pid, process.start_time, snap.boot_id)
        if _target(process):
            return Holder("", pid, process.start_time, snap.boot_id)
        if process.ppid in (0, pid):
            return None
        pid = process.ppid
    return None
