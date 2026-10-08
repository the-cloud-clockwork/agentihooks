"""Drain then restart.

A session launched by `agentihooks init-agent` that started before an install
affecting sessions (a plugin, an MCP registration) is restarted once its turn ends:
killed, then resumed with the same id, name, account and directory. A swarm
session is skipped: the tick binds each agent to its process and replaces it instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
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
    if environ.get(LAUNCH_ENV) != "1" or environ.get("AGENTIHOOKS_SWARM"):
        return False
    return latest_affecting_change(home) > process_started_at(pid)


@dataclass(frozen=True)
class Original:
    session_id: str
    name: str
    cwd: str
    account: str
    pid: int
    profile: str = ""
    model: str = ""
    effort: str = ""

    @classmethod
    def of(cls, session_id: str, name: str, entry: dict, pid: int, environ: dict) -> Original:
        return cls(
            session_id=session_id,
            name=name,
            cwd=entry.get("cwd") or os.getcwd(),
            account=entry.get("account", ""),
            pid=pid,
            profile=environ.get("AGENTIHOOKS_PROFILE", ""),
            model=environ.get("AGENTIHOOKS_RUN_MODEL", ""),
            effort=environ.get("AGENTIHOOKS_RUN_EFFORT", ""),
        )


def restart_commands(original: Original) -> list[list[str]]:
    exe = shutil.which("agentihooks") or "agentihooks"
    launch = [exe, "init-agent", "--agent", "claude", "--dir", original.cwd, "--name", original.name]
    launch += ["--prompt", NOTE]
    if original.profile:
        launch += ["--profile", original.profile]
    launch += ["--"]
    if original.account:
        launch += ["--route", original.account]
    if original.model:
        launch += ["--model", original.model]
    if original.effort:
        launch += ["--effort", original.effort]
    launch += ["--resume", original.session_id]
    kill = [exe, "terminate-agent", str(original.pid), "--type", "claude", "--force-shared"]
    return [kill, launch]


def _closing_marker(pid: int, environ: dict) -> Path:
    from scripts.init_agent import _runtime_dir

    with open(f"/proc/{pid}/stat") as fh:
        ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
    return _runtime_dir(environ) / f"closing-{ppid}"


def live_session_ids() -> list[str]:
    from scripts.terminate_agent import sessions

    return [item.session_id for item in sessions()]


def restart(commands: list[list[str]], marker: Path, session_id: str) -> None:
    kill, launch = commands
    marker.touch()
    done = subprocess.run(kill, stdin=subprocess.DEVNULL, capture_output=True, timeout=300, check=False)
    if done.returncode != 0 or session_id in live_session_ids():
        marker.unlink(missing_ok=True)
        print(f"session-refresh: {session_id} still running, resume skipped: {done.stderr!r}", file=sys.stderr)
        return
    subprocess.run(launch, stdin=subprocess.DEVNULL, capture_output=True, timeout=300, check=False)


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
    commands = restart_commands(Original.of(session_id, name, entry, pid, dict(os.environ)))
    fork_and_call(
        restart,
        commands,
        _closing_marker(pid, dict(os.environ)),
        session_id,
        timeout_sec=600,
        task_name="session-refresh",
    )
    return True
