import hashlib
import os
import time
from pathlib import Path

from hooks.context import project_cache
from hooks.context.project_render import NO_MEMORY
from hooks.targets import current_target
from hooks.targets.capabilities import can_inject_context
from scripts.swarm_v2.keyspace import key, stamp

OUTCOMES = "context_delivery_outcomes_by_target"
RECEIPT_TTL = 14400


def digest(pack: str) -> str:
    return hashlib.sha256(pack.encode()).hexdigest()


def _outcomes_path() -> Path:
    return project_cache._state_dir() / f"{OUTCOMES}.json"


def outcomes() -> dict:
    return project_cache._read(_outcomes_path())


def _record(target: str, outcome: str, session: str = "", pack: str | None = None) -> None:
    from hooks.context.broadcast import _file_lock

    path = _outcomes_path()
    with _file_lock(path):
        counts = project_cache._read(path)
        row = counts.setdefault(target, {})
        row[outcome] = row.get(outcome, 0) + 1
        project_cache._write(path, counts)
    if session:
        from hooks.observability import consultation

        consultation.delivery(session, pack, outcome)


def _receipt_path(session_id: str) -> Path:
    return project_cache._state_dir() / f"{key(project_cache.namespace(), 'receipt', session_id)}.json"


def receipt(session_id: str) -> dict:
    return project_cache._scoped(_receipt_path(session_id), project_cache.namespace(), "receipt")


def _disabled(target: str) -> bool:
    names = os.getenv("AGENTIHOOKS_TASK_CONTEXT_OFF")
    return bool(names) and target in {name.strip() for name in names.split(",")}


def hold(session_id: str, pack: str) -> None:
    project_cache.defer_project_context(session_id, pack)


def binding(swarm: str | None, task: str | None, environ=None) -> dict:
    if not (swarm and task):
        return {}
    from scripts.gates.brain import bind, task_row
    from scripts.swarm_ledger.repository.folder import ledger_folder

    row = task_row(ledger_folder(os.environ if environ is None else environ), swarm, task)
    return bind(task, row, project_cache.namespace().brain) if row else {}


def _consultation_path(session_id: str) -> Path:
    return project_cache._state_dir() / f"{key(project_cache.namespace(), 'consultation', session_id)}.json"


def consultation(session_id: str) -> dict:
    return project_cache._scoped(_consultation_path(session_id), project_cache.namespace(), "consultation")


def classify(pack: str) -> str:
    from scripts.gates.brain import DELIVERED, EMPTY, FAILED

    lines = pack.splitlines()
    if any(line.startswith(project_cache.UNREACHABLE) for line in lines):
        return FAILED
    return EMPTY if NO_MEMORY in lines else DELIVERED


def _consulted(session_id: str, bound: dict) -> bool:
    from scripts.gates.brain import check

    return not bound or not check(consultation(session_id), bound, time.time())


def _consult(session_id: str, bound: dict, pack: str, now: float) -> None:
    ttl = float(os.environ.get("AGENTIHOOKS_BRAIN_RECEIPT_TTL") or RECEIPT_TTL)
    project_cache._write(
        _consultation_path(session_id),
        stamp(
            project_cache.namespace(),
            "consultation",
            {**bound, "result": classify(pack), "digest": digest(pack), "at": now, "expires": now + ttl},
        ),
    )


def offer(session_id: str, event: str, pack: str | None, injectable: bool | None = None) -> str | None:
    if not pack:
        from hooks.observability import consultation as metrics

        bound = binding(os.environ.get("AGENTIHOOKS_SWARM"), os.environ.get("AGENTIHOOKS_SWARM_TASK"))
        if bound and (not consultation(session_id) or not _consulted(session_id, bound)):
            metrics.delivery(session_id, pack, "missing")
        return None
    target = current_target()
    project_cache.take_project_context(session_id)
    if _disabled(target):
        _record(target, "disabled", session_id, pack)
        return None
    bound = binding(os.environ.get("AGENTIHOOKS_SWARM"), os.environ.get("AGENTIHOOKS_SWARM_TASK"))
    if receipt(session_id).get("digest") == digest(pack) and _consulted(session_id, bound):
        _record(target, "deduplicated", session_id, pack)
        return None
    if not (can_inject_context(event) if injectable is None else injectable):
        hold(session_id, pack)
        _record(target, "unsupported_event", session_id, pack)
        return None
    now = time.time()
    project_cache._write(
        _receipt_path(session_id),
        stamp(
            project_cache.namespace(),
            "receipt",
            {"digest": digest(pack), "event": event, "target": target, "at": now},
        ),
    )
    if bound:
        _consult(session_id, bound, pack, now)
    _record(target, "delivered", session_id, pack)
    return pack


def deliver_pending(session_id: str, event: str) -> str | None:
    return offer(session_id, event, project_cache.take_project_context(session_id))


def restart(session_id: str) -> None:
    _receipt_path(session_id).unlink(missing_ok=True)
    project_cache.take_project_context(session_id)
