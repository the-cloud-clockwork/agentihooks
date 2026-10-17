"""A swarm identity belongs to the session its launcher started; a session nested inside another agent drops it."""

import os
from collections.abc import MutableMapping
from pathlib import Path

from hooks.context.account_sessions import _PROC, _comm, _ppid, _sid

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
    agent = 0
    sessions, previous, pid = 0, None, os.getppid() if start is None else start
    while pid > 1 and pid != int(launcher):
        comm = _comm(pid, proc)
        if comm is None:
            return False
        if comm in AGENTS:
            sessions, agent = sessions + (comm != previous), pid
        previous, pid = comm, _ppid(pid, proc)
    if sessions > 1:
        return True
    return pid != int(launcher) and _session_agents(int(launcher), proc) != [agent]


def _session_agents(sid: int, proc: Path) -> list[int]:
    found = []
    for entry in proc.iterdir():
        pid = int(entry.name) if entry.name.isdigit() else 0
        comm = _comm(pid, proc) if pid else None
        if comm in AGENTS and _sid(pid, proc) == sid and _comm(_ppid(pid, proc), proc) != comm:
            found.append(pid)
    return found


def unpin(environ: MutableMapping[str, str] = os.environ, start: int | None = None, proc: Path = _PROC) -> bool:
    if not nested(environ, start, proc):
        return False
    for name in IDENTITY:
        environ.pop(name, None)
    return True
