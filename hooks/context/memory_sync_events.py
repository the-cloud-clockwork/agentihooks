"""Event-driven memory-mirror hooks (v5).

Non-blocking model — uses the shared ``hooks._async.fork_and_call`` helper:

- ``UserPromptSubmit`` → fork → grandchild runs
  ``scripts.memory_mirror_sync.pull_only`` in-process. Hook returns <10ms.
- ``PostToolUse`` → mark session dirty when a Write/Edit lands inside
  ``~/.claude/projects/<enc>/memory/``. Synchronous file touch, ~1ms.
- ``Stop`` → if session is dirty AND role=contributor, fork → grandchild
  runs ``propose_pr`` in-process. Hook returns <10ms.

Authority role continues to use the 60s daemon in ``scripts/sync_daemon.py``.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

from hooks._async import fork_and_call
from hooks.common import log


_MEMORY_PATH_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")
_VALID_ROLES_PULL = ("consumer", "contributor", "authority")
_MEMORY_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_DIRTY_DIR = Path.home() / ".agentihooks" / "state" / "memory_dirty"


# Re-export for tests that monkeypatch this symbol.
_fork_and_call = fork_and_call


def _agent_name() -> str:
    return (
        os.getenv("AGENTICORE_AGENT_NAME")
        or os.getenv("AGENT_NAME")
        or socket.gethostname()
    )


def on_user_prompt(payload: dict) -> None:
    """UserPromptSubmit handler — fork-and-call pull_only for any active role."""
    try:
        from hooks.config import MEMORY_MIRROR_REMOTE, MEMORY_MIRROR_ROLE
    except ImportError:
        return
    if MEMORY_MIRROR_ROLE not in _VALID_ROLES_PULL:
        return
    if not (MEMORY_MIRROR_REMOTE or "").strip():
        return
    try:
        from scripts.memory_mirror_sync import pull_only
    except ImportError as e:
        log("memory_sync: import pull_only failed", {"error": str(e)})
        return
    _fork_and_call(pull_only, timeout_sec=120, task_name="pull")


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
    try:
        from scripts.memory_mirror_sync import propose_pr
    except ImportError as e:
        log("memory_sync: import propose_pr failed", {"error": str(e)})
        return
    # Delete the flag BEFORE forking — otherwise the grandchild and parent
    # race on the same file and if the user starts a new session before the
    # grandchild finishes, the stale flag could trigger a duplicate propose.
    try:
        flag.unlink()
    except OSError:
        pass
    _fork_and_call(
        propose_pr,
        timeout_sec=180,
        task_name="propose",
        session_id=short,
        agent_name=agent,
    )
