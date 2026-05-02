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
import time

from hooks._async import fork_and_call
from hooks.common import log

_MEMORY_PATH_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")
_VALID_ROLES_PULL = ("consumer", "contributor", "authority")
_MEMORY_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
from hooks.config import AGENTIHOOKS_HOME

_DIRTY_DIR = AGENTIHOOKS_HOME / "state" / "memory_dirty"
_DIRTY_TTL_SEC = 7 * 24 * 3600
_SWEEP_INTERVAL_SEC = 24 * 3600
_SWEEP_STAMP = AGENTIHOOKS_HOME / "state" / "memory_dirty_sweep.stamp"


def _sweep_stale_dirty_flags() -> None:
    """Remove dirty-flag files older than _DIRTY_TTL_SEC. Throttled to once/day.

    Called from on_user_prompt so any active role does housekeeping. Cheap:
    one stat on the stamp, one listdir on a tiny directory.
    """
    try:
        now = time.time()
        try:
            if now - _SWEEP_STAMP.stat().st_mtime < _SWEEP_INTERVAL_SEC:
                return
        except FileNotFoundError:
            pass
        if not _DIRTY_DIR.exists():
            _SWEEP_STAMP.parent.mkdir(parents=True, exist_ok=True)
            _SWEEP_STAMP.touch()
            return
        removed = 0
        cutoff = now - _DIRTY_TTL_SEC
        for f in _DIRTY_DIR.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        _SWEEP_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _SWEEP_STAMP.touch()
        if removed:
            log("memory_sync: swept stale dirty flags", {"removed": removed})
    except Exception as e:
        log("memory_sync: sweep failed", {"error": str(e)})


# Re-export for tests that monkeypatch this symbol.
_fork_and_call = fork_and_call


def _agent_name() -> str:
    return os.getenv("AGENTICORE_AGENT_NAME") or os.getenv("AGENT_NAME") or socket.gethostname()


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
    _sweep_stale_dirty_flags()
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
        flag = _DIRTY_DIR / session_id
        # Append one path per line; deduplicate so repeated writes to the
        # same file don't bloat the flag.
        existing: set[str] = set()
        if flag.exists():
            existing = {line.strip() for line in flag.read_text().splitlines() if line.strip()}
        if fp not in existing:
            with flag.open("a") as fh:
                fh.write(fp + "\n")
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
    # Read the list of memory paths touched during the session. Deduplicated
    # line-per-path format written by on_post_tool.
    try:
        touched = [line.strip() for line in flag.read_text().splitlines() if line.strip()]
    except OSError:
        touched = []
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
        touched_paths=touched,
    )
