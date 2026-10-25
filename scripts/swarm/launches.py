"""The process select-profile runs a swarm agent's launch in, recorded at its start under the agent's name so
retirement can end it when the spawn recorded no validated process id."""

import json
import os
from pathlib import Path

from hooks.proc import Process, processes


def home() -> Path:
    return Path.home() / ".agentihooks" / "swarm"


def _path(name: str, root: Path | None) -> Path:
    return (home() if root is None else root) / "launches" / f"{name}.json"


def record(environ: dict[str, str], root: Path | None = None) -> None:
    name = environ.get("AGENTIHOOKS_AGENT_NAME")
    if not environ.get("AGENTIHOOKS_SWARM") or not name:
        return
    own = processes().get(os.getpid())
    if own is None:
        return
    target = _path(name, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"pid": own.pid, "start_time": own.start_time}))


def find(name: str, root: Path | None = None) -> Process | None:
    try:
        data = json.loads(_path(name, root).read_text())
        pid, start = data["pid"], data["start_time"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    found = processes().get(pid)
    return found if found is not None and found.start_time == start else None
