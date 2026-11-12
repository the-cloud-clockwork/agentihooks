"""Close the old terminal after a quota handoff.

Once a session has handed its work to a new one, its next stop terminates it: a
herdr pane is closed by terminate-agent, a native tab by the launcher's closing
marker. AGENTIHOOKS_HANDOFF_CLOSE_OLD=0 keeps the old terminal open.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hooks.lifecycle.guard import _home
from hooks.lifecycle.refresh import _closing_marker
from scripts.quick.member import Member
from scripts.quick.member import find as find_member
from scripts.quick.registry import PENDING

CLOSE_ENV = "AGENTIHOOKS_HANDOFF_CLOSE_OLD"


def due(payload: dict, environ: dict, status: str) -> bool:
    if payload.get("agent_id") or payload.get("stop_hook_active"):
        return False
    return environ.get(CLOSE_ENV, "1") != "0" and status == "handed_off"


def handoff_record(pid: int, session_id: str) -> Path:
    return _home() / f"handed-off-{pid}-{session_id}"


def close_command(pid: int, keep_pane: bool = False) -> list[str]:
    exe = shutil.which("agentihooks") or "agentihooks"
    return [exe, "terminate-agent", str(pid), "--type", "any", *(["--keep-pane"] if keep_pane else [])]


def quick_pending(environ: Mapping[str, str]) -> Member | None:
    """This session's quick seat when it handed off and still occupies the seat's pane."""
    found = find_member(environ)
    if found is None or found.seat.state != PENDING or found.seat.pane_id != environ.get("HERDR_PANE_ID"):
        return None
    return found


def relaunch_command(found: Member) -> list[str]:
    exe = shutil.which("agentihooks") or "agentihooks"
    return [exe, "quick", "relaunch", found.seat.seat, "--folder", str(found.folder)]


def close_and_relaunch(close_argv: list[str], relaunch_argv: list[str]) -> None:
    """End the handed-off quick agent with its pane kept, then start its successor in that pane."""
    subprocess.run(close_argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
    subprocess.run(relaunch_argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=600, check=False)


def close(command: list[str], marker: Path, record: Path) -> None:
    marker.touch()
    done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
    if done.returncode == 0:
        record.unlink(missing_ok=True)


def _claimed(pid: int, session_id: str) -> bool:
    claim = _home() / f"handoff-close-{pid}-{session_id}.claim"
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def on_stop(payload: dict[str, Any]) -> bool:
    from hooks._async import fork_and_call
    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import _load_sessions

    session_id = str(payload.get("session_id", ""))
    pid = agent_pid()
    nested = bool(payload.get("agent_id") or payload.get("stop_hook_active"))
    quick = quick_pending(os.environ) if session_id and not nested else None
    recorded = session_id and handoff_record(pid, session_id).exists()
    status = "handed_off" if recorded else _load_sessions().get(session_id, {}).get("status")
    if quick is None and (not session_id or not due(payload, dict(os.environ), status)):
        return False
    if not _claimed(pid, session_id):
        return False
    if quick is not None:
        job = (close_and_relaunch, close_command(pid, keep_pane=True), relaunch_command(quick))
        fork_and_call(*job, timeout_sec=720, task_name="quick-handoff-close")
        return True
    fork_and_call(
        close,
        close_command(pid),
        _closing_marker(pid, dict(os.environ)),
        handoff_record(pid, session_id),
        timeout_sec=180,
        task_name="handoff-close",
    )
    return True
