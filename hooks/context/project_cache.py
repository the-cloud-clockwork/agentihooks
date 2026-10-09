import hashlib
import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path

from hooks.context.project_identity import ProjectIdentity, resolve_project
from hooks.context.project_memory import ProjectMemory, VaultProjectSource
from hooks.context.project_render import render_project_block
from hooks.context.project_sessions import lookup
from scripts.swarm_v2.keyspace import Namespace, admits, installation, key, stamp, sweep_legacy


def _state_dir() -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return Path(AGENTIHOOKS_HOME) / "brain" / "project-memory"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value))
    temp.replace(path)


def namespace() -> Namespace:
    from hooks.config import AGENTIHOOKS_HOME
    from hooks.context.brain_adapter import brain_id

    return Namespace(installation(Path(AGENTIHOOKS_HOME)).installation_id, brain_id())


def _mismatch_path() -> Path:
    return _state_dir() / "cache-scope-mismatches.json"


def cache_scope_mismatch_total() -> int:
    return sum(_read(_mismatch_path()).values())


def _scoped(path: Path, scope: Namespace, kind: str) -> dict:
    document = _read(path)
    if admits(document, scope, kind):
        return document
    if "namespace" in document:
        from hooks.common import log
        from hooks.context.broadcast import _file_lock

        log("brain_project: cache scope mismatch", {"kind": kind})
        with _file_lock(_mismatch_path()):
            counts = _read(_mismatch_path())
            counts[kind] = counts.get(kind, 0) + 1
            _write(_mismatch_path(), counts)
    return {}


def _feed_path(scope: Namespace) -> Path:
    return _state_dir() / f"{key(scope, 'feed')}.json"


def store_feed(entries: list) -> None:
    from hooks.context.broadcast import _file_lock

    scope = namespace()
    path = _feed_path(scope)
    rows = [asdict(entry) for entry in entries]
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    with _file_lock(path):
        _write(path, stamp(scope, "feed", {"hash": digest, "entries": rows}))
    sweep_legacy(_state_dir())


def _cache_path(identity: ProjectIdentity, scope: Namespace) -> Path:
    return _state_dir() / f"{key(scope, 'project-memory', identity.remote or identity.repo)}.json"


def _fresh(cached: dict, feed_hash: str) -> bool:
    return cached.get("feed_hash") == feed_hash and time.time() - cached.get("at", 0) < float(
        os.getenv("BRAIN_PROJECT_MEMORY_TTL", "600")
    )


def refresh_project_cache(identity: ProjectIdentity, rows: list[dict], feed_hash: str, scope: Namespace) -> None:
    from hooks.common import log
    from hooks.context.brain_adapter import BrainEntry, BrainSourceUnavailable
    from hooks.context.broadcast import _file_lock

    brain = replace(scope, project="")
    path = _cache_path(identity, scope)
    with _file_lock(path):
        if _scoped(_feed_path(brain), brain, "feed").get("hash") != feed_hash:
            return
        if _fresh(_scoped(path, scope, "project-memory"), feed_hash):
            return
        try:
            memory = VaultProjectSource([BrainEntry(**row) for row in rows]).fetch(identity)
        except BrainSourceUnavailable as error:
            log("brain_project: refresh failed", {"error": str(error)})
            return
        if _scoped(_feed_path(brain), brain, "feed").get("hash") == feed_hash:
            _write(
                path,
                stamp(scope, "project-memory", {"feed_hash": feed_hash, "at": time.time(), "memory": asdict(memory)}),
            )


def _swarm_overview() -> str:
    swarm = os.getenv("AGENTIHOOKS_SWARM", "")
    if not swarm:
        return ""
    from scripts.swarm_ledger.repository.folder import ledger_folder
    from scripts.swarm_ledger.repository.sqlite import read_ledger

    ledger = read_ledger(ledger_folder(os.environ), swarm, "overview") or {}
    return str(ledger.get("overview", ""))


def project_context(session_id: str, cwd: str | None = None) -> str | None:
    if os.getenv("BRAIN_PROJECT_SCOPE", "strict") == "off":
        return None
    from hooks.context.broadcast import _load_sessions

    sessions = _load_sessions()
    if cwd is not None:
        identity = resolve_project(cwd)
    elif session_id in sessions:
        row = sessions[session_id].get("project")
        identity = ProjectIdentity(**row) if row else None
    else:
        identity = lookup(session_id)
    if not identity:
        return None
    from hooks._async import fork_and_call
    from hooks.config import BRAIN_PAYLOAD_MAX_BYTES

    brain = namespace()
    scope = replace(brain, project=identity.project_id)
    feed = _scoped(_feed_path(brain), brain, "feed")
    cached = _scoped(_cache_path(identity, scope), scope, "project-memory")
    if feed and not _fresh(cached, feed["hash"]):
        fork_and_call(
            refresh_project_cache,
            identity,
            feed["entries"],
            feed["hash"],
            scope,
            timeout_sec=180,
            task_name="brain_project",
        )
    memory = ProjectMemory(**cached["memory"]) if cached.get("memory") else ProjectMemory(identity.project)
    block = render_project_block(memory, BRAIN_PAYLOAD_MAX_BYTES)
    overview = _swarm_overview()
    if overview:
        block = block.replace("\n\n", f"\n\nPlan: {overview}\n", 1)
    return block.encode()[:BRAIN_PAYLOAD_MAX_BYTES].decode(errors="ignore")


def _pending_path(session_id: str, scope: Namespace) -> Path:
    return _state_dir() / f"{key(scope, 'pending', session_id)}.json"


def defer_project_context(session_id: str, context: str) -> None:
    from hooks.context.broadcast import _file_lock

    scope = namespace()
    path = _pending_path(session_id, scope)
    with _file_lock(path):
        _write(path, stamp(scope, "pending", {"context": context}))


def take_project_context(session_id: str) -> str | None:
    from hooks.context.broadcast import _file_lock

    scope = namespace()
    path = _pending_path(session_id, scope)
    with _file_lock(path):
        pending = _scoped(path, scope, "pending")
        if pending:
            path.unlink()
    return pending.get("context")
