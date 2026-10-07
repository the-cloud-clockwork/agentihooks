"""A swarm identity belongs to the session its launcher started; a session nested inside another agent drops it."""

import os
from collections.abc import MutableMapping
from pathlib import Path

from hooks.context.account_sessions import _PROC, _comm, _ppid, agent_pid

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
    launcher = environ.get(LAUNCHER, "")
    if not launcher.isdigit():
        return False
    sessions, inside, pid = 0, False, agent_pid(start, proc)
    while pid > 1 and pid != int(launcher):
        comm = _comm(pid, proc)
        if comm is None:
            return False
        agent = comm in AGENTS
        sessions += agent and not inside
        inside, pid = agent, _ppid(pid, proc)
    return sessions > 1 or pid != int(launcher)


def unpin(environ: MutableMapping[str, str] = os.environ, start: int | None = None, proc: Path = _PROC) -> bool:
    if not nested(environ, start, proc):
        return False
    for name in IDENTITY:
        environ.pop(name, None)
    return True
