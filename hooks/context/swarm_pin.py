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
    sessions, previous, agent, pid = 0, None, 0, os.getppid() if start is None else start
    while pid > 1 and pid != int(launcher):
        comm = _comm(pid, proc)
        if comm is None:
            return False
        if comm in AGENTS and comm != previous:
            sessions, agent = sessions + 1, pid
        previous, pid = comm, _ppid(pid, proc)
    if sessions > 1:
        return True
    return pid != int(launcher) and _sid(agent, proc) != int(launcher)


def _sid(pid: int, proc: Path) -> int:
    try:
        return int((proc / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[3])
    except (OSError, ValueError, IndexError):
        return 0


def unpin(environ: MutableMapping[str, str] = os.environ, start: int | None = None, proc: Path = _PROC) -> bool:
    if not nested(environ, start, proc):
        return False
    for name in IDENTITY:
        environ.pop(name, None)
    return True
