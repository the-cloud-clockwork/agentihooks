import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from hooks.context.project_identity import ProjectIdentity, resolve_project
from hooks.context.project_memory import ProjectMemory, VaultProjectSource
from hooks.context.project_render import render_project_block
from hooks.context.project_sessions import lookup


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


def store_feed(entries: list) -> None:
    from hooks.context.broadcast import _file_lock

    path = _state_dir() / "feed.json"
    rows = [asdict(entry) for entry in entries]
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    with _file_lock(path):
        _write(path, {"hash": digest, "entries": rows})


def _cache_path(identity: ProjectIdentity) -> Path:
    key = hashlib.sha256((identity.remote or identity.repo).encode()).hexdigest()[:24]
    return _state_dir() / f"{key}.json"


def _fresh(cached: dict, feed_hash: str) -> bool:
    return cached.get("feed_hash") == feed_hash and time.time() - cached.get("at", 0) < float(
        os.getenv("BRAIN_PROJECT_MEMORY_TTL", "600")
    )


def refresh_project_cache(identity: ProjectIdentity, rows: list[dict], feed_hash: str) -> None:
    from hooks.common import log
    from hooks.context.brain_adapter import BrainEntry, BrainSourceUnavailable
    from hooks.context.broadcast import _file_lock

    path = _cache_path(identity)
    with _file_lock(path):
        if _read(_state_dir() / "feed.json").get("hash") != feed_hash:
            return
        if _fresh(_read(path), feed_hash):
            return
        try:
            memory = VaultProjectSource([BrainEntry(**row) for row in rows]).fetch(identity)
        except BrainSourceUnavailable as error:
            log("brain_project: refresh failed", {"error": str(error)})
            return
        if _read(_state_dir() / "feed.json").get("hash") == feed_hash:
            _write(path, {"feed_hash": feed_hash, "at": time.time(), "memory": asdict(memory)})


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

    feed = _read(_state_dir() / "feed.json")
    cached = _read(_cache_path(identity))
    if feed and not _fresh(cached, feed["hash"]):
        fork_and_call(
            refresh_project_cache, identity, feed["entries"], feed["hash"], timeout_sec=180, task_name="brain_project"
        )
    memory = ProjectMemory(**cached["memory"]) if cached.get("memory") else ProjectMemory(identity.project)
    block = render_project_block(memory, BRAIN_PAYLOAD_MAX_BYTES)
    overview = _swarm_overview()
    if overview:
        block = block.replace("\n\n", f"\n\nPlan: {overview}\n", 1)
    return block.encode()[:BRAIN_PAYLOAD_MAX_BYTES].decode(errors="ignore")


def defer_project_context(session_id: str, context: str) -> None:
    path = _state_dir() / f"pending-{hashlib.sha256(session_id.encode()).hexdigest()[:24]}.json"
    from hooks.context.broadcast import _file_lock

    with _file_lock(path):
        _write(path, {"context": context})


def take_project_context(session_id: str) -> str | None:
    path = _state_dir() / f"pending-{hashlib.sha256(session_id.encode()).hexdigest()[:24]}.json"
    from hooks.context.broadcast import _file_lock

    with _file_lock(path):
        context = _read(path).get("context")
        if path.exists():
            path.unlink()
    return context
