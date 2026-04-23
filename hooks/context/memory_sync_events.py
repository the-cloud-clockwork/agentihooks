"""Event-driven memory-mirror hooks (v5).

Replaces the 60s polling tick for consumer + contributor roles:

- ``UserPromptSubmit`` → fire-and-forget ``agentihooks memory-sync pull``.
  One pull per user message. Sub-second fetch; by the time the agent turns
  around to answer, memory is (best-effort) fresh.
- ``PostToolUse`` → mark the session dirty when a Write/Edit lands inside
  ``~/.claude/projects/<enc>/memory/``. Near-zero cost (one file touch).
- ``Stop`` → if session is dirty AND role=contributor, fire-and-forget
  ``agentihooks memory-sync propose --session-id <sid> --agent-name <name>``.
  Opens a PR from branch ``memory/<agent>/<sid>`` to main.

Authority role continues to use the 60s daemon in ``scripts/sync_daemon.py``
— hooks never trigger authority pushes (daemon covers off-session edits
like vim / Obsidian).

All subprocess spawns are wrapped in ``/usr/bin/timeout`` + ``setsid`` so
children can neither hang forever nor pile up as zombies. Init (pid 1)
reaps orphans.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

from hooks.common import log


_MEMORY_PATH_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")
_VALID_ROLES_PULL = ("consumer", "contributor", "authority")
_MEMORY_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_DIRTY_DIR = Path.home() / ".agentihooks" / "state" / "memory_dirty"
_LOG_FILE = Path.home() / ".agentihooks" / "logs" / "memory-sync-hooks.log"


def _spawn_memory_sync(action: str, *args: str, timeout_sec: int = 120) -> None:
    """Fire-and-forget ``agentihooks memory-sync <action>`` subprocess.

    Wrapped in ``/usr/bin/timeout`` so the child dies within ``timeout_sec +
    10s`` no matter what. ``start_new_session=True`` detaches from the
    hook's process group; when the hook exits the child reparents to init
    and gets reaped. No zombies, no accumulation.
    """
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "timeout",
        "--kill-after=10",
        str(timeout_sec),
        "agentihooks",
        "memory-sync",
        action,
        *args,
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=open(_LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, OSError) as e:
        # ``agentihooks`` or ``timeout`` binary missing — silent no-op.
        # Laptop + agentihooks-installed pods always have both.
        log("memory_sync: spawn skipped", {"action": action, "error": str(e)})


def _agent_name() -> str:
    return (
        os.getenv("AGENTICORE_AGENT_NAME")
        or os.getenv("AGENT_NAME")
        or socket.gethostname()
    )


def on_user_prompt(payload: dict) -> None:
    """UserPromptSubmit handler — fire pull for any active role."""
    try:
        from hooks.config import MEMORY_MIRROR_REMOTE, MEMORY_MIRROR_ROLE
    except ImportError:
        return
    if MEMORY_MIRROR_ROLE not in _VALID_ROLES_PULL:
        return
    if not (MEMORY_MIRROR_REMOTE or "").strip():
        return
    _spawn_memory_sync("pull", timeout_sec=120)


def on_post_tool(payload: dict) -> None:
    """PostToolUse handler — mark session dirty when memory file is written.

    Only relevant for contributor (authority uses its daemon, but we still
    mark dirty so manual ``memory-sync sync-now`` invocations know there's
    pending work). Consumer never needs this.
    """
    try:
        from hooks.config import MEMORY_MIRROR_ROLE
    except ImportError:
        return
    if MEMORY_MIRROR_ROLE not in ("contributor", "authority"):
        return
    tool = payload.get("tool_name")
    if tool not in _MEMORY_WRITE_TOOLS:
        return
    fp = (payload.get("tool_input") or {}).get("file_path", "")
    if not fp or not _MEMORY_PATH_RE.search(fp):
        return
    session_id = payload.get("session_id", "") or "unknown"
    try:
        _DIRTY_DIR.mkdir(parents=True, exist_ok=True)
        (_DIRTY_DIR / session_id).write_text(fp)
    except OSError as e:
        log("memory_sync: dirty-mark failed", {"error": str(e)})


def on_stop(payload: dict) -> None:
    """Stop handler — if dirty + contributor, fire propose.

    Authority does NOT fire here — its 60s daemon owns the push path,
    because memory can change outside Claude sessions (vim, Obsidian).
    """
    try:
        from hooks.config import MEMORY_MIRROR_REMOTE, MEMORY_MIRROR_ROLE
    except ImportError:
        return
    if MEMORY_MIRROR_ROLE != "contributor":
        return
    if not (MEMORY_MIRROR_REMOTE or "").strip():
        return
    session_id = payload.get("session_id", "") or "unknown"
    flag = _DIRTY_DIR / session_id
    if not flag.exists():
        return
    agent = _agent_name()
    short = session_id[:8] if session_id and session_id != "unknown" else "adhoc"
    _spawn_memory_sync(
        "propose",
        "--session-id",
        short,
        "--agent-name",
        agent,
        timeout_sec=180,
    )
    try:
        flag.unlink()
    except OSError:
        pass
