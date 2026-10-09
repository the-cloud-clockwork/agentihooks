from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from hooks.context import conditions
from hooks.filters import extract


def session(payload: dict) -> str:
    return str(payload.get("session_id") or os.environ.get("AGENTIHOOKS_AGENT_NAME", ""))


def target(payload: dict, where: tuple) -> str:
    path = extract.target_path(payload.get("tool_input") or {})
    if path:
        return str((Path(payload.get("cwd") or Path.cwd()) / path).resolve())
    return json.dumps([payload.get("tool_name"), where])


def _path(entry: dict, payload: dict, target: str) -> Path:
    identity = json.dumps([session(payload), entry["path"], target])
    key = hashlib.sha256(identity.encode()).hexdigest()
    return conditions.runtime_dir() / "filters" / "rounds" / key


def send_back(entry: dict, payload: dict, target: str, maximum: int) -> int:
    path = _path(entry, payload, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as counter:
        fcntl.flock(counter, fcntl.LOCK_EX)
        counter.seek(0)
        count = int(counter.read() or "0")
        if count < maximum:
            counter.seek(0)
            counter.truncate()
            counter.write(str(count + 1))
        return count


def reset(entry: dict, payload: dict, target: str) -> None:
    path = _path(entry, payload, target)
    if path.exists():
        with path.open("r+") as counter:
            fcntl.flock(counter, fcntl.LOCK_EX)
            counter.truncate()
