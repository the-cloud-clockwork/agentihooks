"""Close the old terminal after a quota handoff.

Once a session has handed its work to a new one, its next stop terminates it: a
herdr pane is closed by terminate-agent, a native tab by the launcher's closing
marker. AGENTIHOOKS_HANDOFF_CLOSE_OLD=0 keeps the old terminal open.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hooks.lifecycle.guard import _home
from hooks.lifecycle.refresh import _closing_marker

CLOSE_ENV = "AGENTIHOOKS_HANDOFF_CLOSE_OLD"


def due(payload: dict, environ: dict, status: str) -> bool:
    if payload.get("agent_id") or payload.get("stop_hook_active"):
        return False
    return environ.get(CLOSE_ENV, "1") != "0" and status == "handed_off"


def handoff_record(pid: int, session_id: str) -> Path:
    return _home() / f"handed-off-{pid}-{session_id}"


def close_command(pid: int) -> list[str]:
    exe = shutil.which("agentihooks") or "agentihooks"
    return [exe, "terminate-agent", str(pid), "--type", "any"]


def close(command: list[str], marker: Path, record: Path) -> None:
    marker.touch()
    done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
    if done.returncode == 0:
        record.unlink(missing_ok=True)


def on_stop(payload: dict) -> bool:
    from hooks._async import fork_and_call
    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import _load_sessions

    session_id = payload.get("session_id", "")
    pid = agent_pid()
    recorded = session_id and handoff_record(pid, session_id).exists()
    status = "handed_off" if recorded else _load_sessions().get(session_id, {}).get("status")
    if not session_id or not due(payload, dict(os.environ), status):
        return False
    claim = _home() / f"handoff-close-{pid}-{session_id}.claim"
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    fork_and_call(
        close,
        close_command(pid),
        _closing_marker(pid, dict(os.environ)),
        handoff_record(pid, session_id),
        timeout_sec=180,
        task_name="handoff-close",
    )
    return True
