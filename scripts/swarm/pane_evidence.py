import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from hooks.secrets import redact
from scripts.hive import reports

SCREEN_LIMIT = 64_000
RECORD_LIMIT = 192
ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def sweep(slug: str, store, runtime, at: int) -> list[str]:
    try:
        return _sweep(slug, store, runtime, at)
    except Exception as error:
        return [f"pane evidence sweep failed: {type(error).__name__}"]


def _sweep(slug: str, store, runtime, at: int) -> list[str]:
    if not callable(getattr(runtime, "herdr", None)):
        return []
    directory = runtime.home / slug / "pane-evidence"
    deadline = time.monotonic() + 8
    agents = [
        agent
        for agent in store.agents(slug)
        if agent.runtime_backend == "local" and not reports.host(agent) and agent.pane_id
    ]
    actions = []
    for agent in sorted(agents, key=lambda agent: _last_capture(directory, agent)):
        if time.monotonic() >= deadline:
            actions.append("pane evidence capture budget exhausted")
            break
        try:
            actions += capture(directory, runtime, agent, at)
        except Exception as error:
            actions.append(f"pane evidence failed for {agent.name}: {type(error).__name__}")
    return actions


def _latest(directory: Path, agent) -> Path:
    stem = hashlib.sha256(agent.name.encode()).hexdigest()
    return directory / f"{stem}.latest.json"


def _last_capture(directory: Path, agent) -> int:
    latest = _latest(directory, agent)
    return latest.stat().st_mtime_ns if latest.exists() else 0


def _call(runtime, args: list[str]) -> dict:
    from scripts.herdr_host import binary
    from scripts.swarm.runtime import herdr_call

    if runtime.herdr is not herdr_call:
        return runtime.herdr(args)
    done = subprocess.run([binary(), *args], capture_output=True, text=True, timeout=2, check=True)
    if args[:2] == ["pane", "read"]:
        return {"text": done.stdout}
    return json.loads(done.stdout)["result"]


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
            stream.write(json.dumps(row))
            stream.flush()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def capture(directory: Path, runtime, agent, at: int) -> list[str]:
    from functools import partial

    from scripts.swarm.runtime import _owns

    herdr = partial(_call, runtime)
    reply, lookup_failed = _read(herdr, ["agent", "get", agent.pane_id])
    found = reply.get("agent", reply)
    if not lookup_failed and (not found or not _owns(found, agent)):
        return []
    if lookup_failed:
        row = {
            "at": at,
            "agent": agent.name,
            "task": agent.task,
            "pane": {"pane_id": agent.pane_id},
            "screen_ansi": "",
            "screen_text": "",
            "process": {},
            "reason": f"pane lookup failed: {lookup_failed}",
            "process_error": "",
        }
    else:
        row = _snapshot(herdr, agent, found, at)
    return _save(directory, agent, row)


def _snapshot(herdr, agent, found: dict, at: int) -> dict:
    screen, failed = _read(herdr, ["pane", "read", agent.pane_id, "--source", "visible", "--format", "ansi"])
    process, process_failed = _read(herdr, ["pane", "process-info", "--pane", agent.pane_id])
    text = screen.get("text")
    state = found.get("agent_status", "unknown")
    reason = f"screen read failed: {failed}" if failed else _reason(text, state)
    plain = ESCAPE.sub("", text) if isinstance(text, str) else ""
    safe_plain = redact(plain, mode="strict")
    safe = redact(text, mode="strict") if isinstance(text, str) and safe_plain == plain else safe_plain
    return {
        "at": at,
        "agent": agent.name,
        "task": agent.task,
        "pane": {key: found[key] for key in ("pane_id", "terminal_id", "agent_status") if key in found},
        "screen_ansi": safe[:SCREEN_LIMIT],
        "screen_text": safe_plain[:SCREEN_LIMIT],
        "reason": reason,
        "process": _process(process.get("process_info", {})),
        "process_error": process_failed,
    }


def _save(directory: Path, agent, row: dict) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    latest = _latest(directory, agent)
    previous = latest.with_name(latest.name.replace(".latest.", ".previous."))
    incident = latest.with_name(latest.name.replace(".latest.", ".incident."))
    before = json.loads(latest.read_text()) if latest.exists() else None
    if before is not None:
        latest.replace(previous)
    _write(latest, row)
    actions = []
    if row["reason"] and (before is None or not before["reason"]):
        _write(incident, {**row, "previous": before})
        actions.append(f"pane evidence saved for {agent.name}: {row['reason']}")
    records = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for expired in records[RECORD_LIMIT:]:
        expired.unlink()
    return actions
