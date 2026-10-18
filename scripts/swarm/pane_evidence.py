import hashlib
import json
import re
import tempfile
from pathlib import Path

from hooks.secrets import redact
from scripts.hive import reports

SCREEN_LIMIT = 64_000
RECORD_LIMIT = 192
ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def sweep(slug: str, store, runtime, at: int) -> list[str]:
    if not callable(getattr(runtime, "herdr", None)):
        return []
    directory = runtime.home / slug / "pane-evidence"
    actions = []
    for agent in store.agents(slug):
        if agent.runtime_backend != "local" or reports.host(agent) or not agent.pane_id:
            continue
        try:
            actions += capture(directory, runtime, agent, at)
        except Exception as error:
            actions.append(f"pane evidence failed for {agent.name}: {type(error).__name__}")
    return actions


def _read(herdr, args: list[str]) -> tuple[dict, str]:
    try:
        return herdr(args), ""
    except Exception as error:
        return {}, type(error).__name__


def _reason(text, state: str) -> str:
    if not isinstance(text, str):
        return "missing screen"
    plain = ESCAPE.sub("", text)
    if not plain.strip():
        return "blank screen"
    if "\ufffd" in plain or "\x00" in plain:
        return "invalid screen text"
    if state == "unknown":
        return "unknown pane state"
    return ""


def _process(info: dict) -> dict:
    return {
        "shell_pid": info.get("shell_pid"),
        "foreground_process_group_id": info.get("foreground_process_group_id"),
        "foreground_processes": [
            {key: process.get(key) for key in ("pid", "name", "cwd")}
            for process in info.get("foreground_processes", [])
        ],
    }


def _write(path: Path, row: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(json.dumps(row, ensure_ascii=False))
            stream.flush()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def capture(directory: Path, runtime, agent, at: int) -> list[str]:
    from scripts.swarm.runtime import _owns

    found = runtime._get(agent.pane_id)
    if found is None or not _owns(found, agent):
        return []
    screen, failed = _read(runtime.herdr, ["pane", "read", agent.pane_id, "--source", "visible", "--format", "ansi"])
    process, process_failed = _read(runtime.herdr, ["pane", "process-info", "--pane", agent.pane_id])
    text = screen.get("text")
    state = found.get("agent_status", "unknown")
    reason = f"screen read failed: {failed}" if failed else _reason(text, state)
    safe = redact(text)[:SCREEN_LIMIT] if isinstance(text, str) else ""
    row = {
        "at": at,
        "agent": agent.name,
        "task": agent.task,
        "pane": {key: found[key] for key in ("pane_id", "terminal_id", "agent_status") if key in found},
        "screen_ansi": safe,
        "screen_text": ESCAPE.sub("", safe),
        "reason": reason,
        "process": _process(process.get("process_info", {})),
        "process_error": process_failed,
    }
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = hashlib.sha256(agent.name.encode()).hexdigest()[:16]
    latest = directory / f"{stem}.latest.json"
    previous = directory / f"{stem}.previous.json"
    incident = directory / f"{stem}.incident.json"
    before = json.loads(latest.read_text()) if latest.exists() else None
    if before is not None:
        latest.replace(previous)
    _write(latest, row)
    actions = []
    if reason and (before is None or before["reason"] != reason):
        _write(incident, {**row, "previous": before})
        actions.append(f"pane evidence saved for {agent.name}: {reason}")
    records = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for expired in records[RECORD_LIMIT:]:
        expired.unlink()
    return actions
