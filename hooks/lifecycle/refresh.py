"""Drain then restart.

A session launched by `agentihooks init-agent` that started before an install
affecting sessions (a plugin, an MCP registration) is restarted once its turn ends:
killed, then resumed with the same id, name, account and directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from hooks.lifecycle.guard import _home

LAUNCH_ENV = "AGENTIHOOKS_TERMINAL_LAUNCH"
NOTE = (
    "Resumed after a dependency or MCP change that needed a restart. "
    "If you use Serena, call activate_project with your worktree's absolute path again. "
    "Re-arm your Monitors, then continue where you stopped."
)


def latest_affecting_change(home: Path) -> float:
    try:
        lines = (home / "deps-installs.jsonl").read_text().splitlines()
    except OSError:
        return 0.0
    stamps = [0.0]
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("ok") and entry.get("affects_sessions"):
            stamps.append(float(entry.get("ts", 0)))
    return max(stamps)


def process_started_at(pid: int) -> float:
    try:
        return os.stat(f"/proc/{pid}").st_ctime
    except OSError:
        return time.time()


def due(payload: dict, environ: dict, home: Path, pid: int) -> bool:
    if payload.get("agent_id") or payload.get("stop_hook_active"):
        return False
    if environ.get(LAUNCH_ENV) != "1":
        return False
    return latest_affecting_change(home) > process_started_at(pid)


def restart_commands(session_id: str, name: str, cwd: str, account: str) -> list[list[str]]:
    exe = shutil.which("agentihooks") or "agentihooks"
    launch = [exe, "init-agent", "--agent", "claude", "--dir", cwd, "--name", name, "--prompt", NOTE, "--"]
    if account:
        launch += ["--route", account]
    launch += ["--resume", session_id]
    return [[exe, "terminate-agent", session_id, "--type", "claude"], launch]


def _closing_marker(pid: int, environ: dict) -> Path:
    from scripts.init_agent import _runtime_dir

    with open(f"/proc/{pid}/stat") as fh:
        ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
    return _runtime_dir(environ) / f"closing-{ppid}"


def restart(commands: list[list[str]], marker: Path) -> None:
    marker.touch()
    for argv in commands:
        subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=300, check=False)


def on_stop(payload: dict) -> bool:
    from hooks._async import fork_and_call
    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import _load_sessions

    pid, home, session_id = agent_pid(), _home(), payload.get("session_id", "")
    if not session_id or not due(payload, dict(os.environ), home, pid):
        return False
    claim = home / f"refresh-{session_id}-{int(latest_affecting_change(home))}.claim"
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    entry = _load_sessions().get(session_id, {})
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    name = cmdline[cmdline.index(b"--name") + 1].decode() if b"--name" in cmdline else session_id[:8]
    commands = restart_commands(session_id, name, entry.get("cwd") or os.getcwd(), entry.get("account", ""))
    fork_and_call(
        restart, commands, _closing_marker(pid, dict(os.environ)), timeout_sec=600, task_name="session-refresh"
    )
    return True
