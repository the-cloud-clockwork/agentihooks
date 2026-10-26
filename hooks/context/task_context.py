import hashlib
import os
import time
from pathlib import Path

from hooks.context import project_cache
from hooks.targets import current_target
from hooks.targets.capabilities import can_inject_context
from scripts.swarm_v2.keyspace import key, stamp

OUTCOMES = "context_delivery_outcomes_by_target"


def digest(pack: str) -> str:
    return hashlib.sha256(pack.encode()).hexdigest()


def _outcomes_path() -> Path:
    return project_cache._state_dir() / f"{OUTCOMES}.json"


def outcomes() -> dict:
    return project_cache._read(_outcomes_path())


def _record(target: str, outcome: str) -> None:
    from hooks.context.broadcast import _file_lock

    path = _outcomes_path()
    with _file_lock(path):
        counts = project_cache._read(path)
        row = counts.setdefault(target, {})
        row[outcome] = row.get(outcome, 0) + 1
        project_cache._write(path, counts)


def _receipt_path(session_id: str) -> Path:
    return project_cache._state_dir() / f"{key(project_cache.namespace(), 'receipt', session_id)}.json"


def receipt(session_id: str) -> dict:
    return project_cache._scoped(_receipt_path(session_id), project_cache.namespace(), "receipt")


def _disabled(target: str) -> bool:
    names = os.getenv("AGENTIHOOKS_TASK_CONTEXT_OFF")
    return bool(names) and target in {name.strip() for name in names.split(",")}


def hold(session_id: str, pack: str) -> None:
    project_cache.defer_project_context(session_id, pack)


def offer(session_id: str, event: str, pack: str | None, injectable: bool | None = None) -> str | None:
    if not pack:
        return None
    target = current_target()
    if _disabled(target):
        _record(target, "disabled")
        return None
    project_cache.take_project_context(session_id)
    if receipt(session_id).get("digest") == digest(pack):
        _record(target, "deduplicated")
        return None
    if not (can_inject_context(event) if injectable is None else injectable):
        hold(session_id, pack)
        _record(target, "unsupported_event")
        return None
    project_cache._write(
        _receipt_path(session_id),
        stamp(
            project_cache.namespace(),
            "receipt",
            {"digest": digest(pack), "event": event, "target": target, "at": time.time()},
        ),
    )
    _record(target, "delivered")
    return pack


def deliver_pending(session_id: str, event: str) -> str | None:
    return offer(session_id, event, project_cache.take_project_context(session_id))


def restart(session_id: str) -> None:
    _receipt_path(session_id).unlink(missing_ok=True)
    project_cache.take_project_context(session_id)
