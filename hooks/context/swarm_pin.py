"""A swarm identity belongs to the session its launcher started; a session nested inside another agent drops it."""

import os
from collections.abc import MutableMapping
from pathlib import Path

from hooks.context.account_sessions import _PROC, _comm, _ppid

LAUNCHER = "AGENTIHOOKS_SWARM_LAUNCHER"
IDENTITY = (
    "AGENTIHOOKS_AGENT_NAME",
    "AGENTIHOOKS_SWARM",
    "AGENTIHOOKS_SWARM_LANE",
    "AGENTIHOOKS_SWARM_TASK",
    LAUNCHER,
)
AGENTS = frozenset({"claude", "codex"})


def nested(environ, start: int | None = None, proc: Path = _PROC) -> bool:
    launcher = str(environ.get(LAUNCHER))
    if not launcher.isdigit():
        return False
    sessions, previous, pid = 0, None, os.getppid() if start is None else start
    while pid > 1 and pid != int(launcher):
        comm = _comm(pid, proc)
        if comm is None:
            return False
        if comm in AGENTS:
            agent = pid
            if comm != previous:
                sessions += 1
        previous, pid = comm, _ppid(pid, proc)
    if sessions == 1 and pid != int(launcher):
        return _session_agents(int(launcher), proc) != [agent]
    return sessions > 1 or pid != int(launcher)


def _session_agents(sid: int, proc: Path) -> list[int]:
    rows = {int(entry.name): row for entry in proc.iterdir() if entry.name.isdigit() and (row := _row(entry))}
    names = {pid: comm for pid, (comm, _, _) in rows.items()}
    return [
        pid
        for pid, (comm, ppid, session) in rows.items()
        if comm in AGENTS and session == sid and names.get(ppid) != comm
    ]


def _row(entry: Path) -> tuple[str, int, int] | None:
    try:
        head, _, tail = (entry / "stat").read_text().rpartition(")")
        fields = tail.split()
        return head.partition("(")[2], int(fields[1]), int(fields[3])
    except (OSError, ValueError, IndexError):
        return None


def unpin(environ: MutableMapping[str, str] = os.environ, start: int | None = None, proc: Path = _PROC) -> bool:
    if not nested(environ, start, proc):
        return False
    for name in IDENTITY:
        environ.pop(name, None)
    return True
