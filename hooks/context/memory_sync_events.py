"""Event-driven memory-mirror hooks (v5).

Non-blocking, no-subprocess model:

- ``UserPromptSubmit`` → double-fork → grandchild imports
  ``scripts.memory_mirror_sync.pull_only`` and runs it. Hook returns in
  <10ms; the grandchild is detached and reparented to init (systemd on
  laptop, tini on pod) which reaps it. No zombies.
- ``PostToolUse`` → mark the session dirty when a Write/Edit lands inside
  ``~/.claude/projects/<enc>/memory/``. One file touch, synchronous, ~1ms.
- ``Stop`` → if session is dirty AND role=contributor, double-fork →
  grandchild runs ``propose_pr`` in-process. No external CLI, no subprocess
  spawning, no blocking. Session cleanup returns immediately.

Why fork instead of subprocess:
- No CLI re-exec (no ~200ms Python interpreter cold-start in the child).
- No resolver complexity (pip install vs ``/shared/agentihooks`` vs PATH).
- No release-gate: new hook behavior lands the moment the agentihooks code
  is on the pod, regardless of what the pip-installed CLI version is.
- Still fire-and-forget: signal.alarm() inside the grandchild enforces a
  hard timeout equivalent to ``/usr/bin/timeout``.

Authority role continues to use the 60s daemon in ``scripts/sync_daemon.py``
— hooks never trigger authority pushes (daemon covers off-session edits
like vim / Obsidian).
"""

from __future__ import annotations

import os
import re
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Callable

from hooks.common import log


_MEMORY_PATH_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")
_VALID_ROLES_PULL = ("consumer", "contributor", "authority")
_MEMORY_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_DIRTY_DIR = Path.home() / ".agentihooks" / "state" / "memory_dirty"
_LOG_FILE = Path.home() / ".agentihooks" / "logs" / "memory-sync-hooks.log"


def _fork_and_call(
    func: Callable[..., Any],
    *args: Any,
    timeout_sec: int = 120,
    task_name: str = "memory-sync",
    **kwargs: Any,
) -> None:
    """Classic double-fork — run ``func(*args, **kwargs)`` in a fully
    detached grandchild process while the caller returns in <10ms.

    Lifecycle:
      1. First fork → parent briefly waitpids the first child (<10ms).
      2. First child setsid() → new session.
      3. Second fork → first child exits immediately.
      4. Grandchild: reparented to init (pid 1) by the kernel. When it
         exits, init reaps it. No zombies even if the caller dies first.

    Safety:
      - ``signal.alarm(timeout_sec)`` kills the grandchild if ``func``
        hangs. Default 120s; callers can override for slower ops (e.g.
        ``propose_pr`` pushes + calls gh pr create — use 180s there).
      - stdout + stderr redirected to ``~/.agentihooks/logs/memory-sync-hooks.log``
        so the child's output is debuggable but never leaks onto the
        hook's stdout (which Claude Code parses).
      - Inherited fds above stderr are closed in the grandchild to avoid
        leaking parent resources (Redis connections, OTel exporter
        sockets, transcript pipes).

    Failures in the caller path (fork fails, etc.) are logged but never
    propagated — hook MUST NOT block or raise.
    """
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = os.fork()
    except OSError as e:
        log("memory_sync: fork failed", {"task": task_name, "error": str(e)})
        return
    if pid > 0:
        # Parent — reap the first child and return. First-child exits
        # almost immediately after its own fork, so this waitpid is
        # sub-millisecond.
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        return

    # First child. Become session leader, then fork again so the grandchild
    # is an orphan (reparented to init).
    try:
        os.setsid()
    except OSError:
        pass
    try:
        pid2 = os.fork()
    except OSError:
        os._exit(0)
    if pid2 > 0:
        os._exit(0)

    # Grandchild. Everything here runs detached. Must never raise back
    # to the caller; always os._exit().
    try:
        # Redirect stdio to the hook log.
        try:
            log_fd = os.open(str(_LOG_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            os.dup2(log_fd, 1)  # stdout
            os.dup2(log_fd, 2)  # stderr
            os.close(log_fd)
            # Drop stdin so git/gh can't hang on a credential prompt.
            with open(os.devnull, "rb") as devnull:
                os.dup2(devnull.fileno(), 0)
        except OSError:
            pass

        # Close any other inherited fds (Redis, OTel, transcript pipes).
        # Start at fd 3 — conservative upper bound of 1024.
        try:
            os.closerange(3, 1024)
        except OSError:
            pass

        # Hard timeout via SIGALRM. If ``func`` doesn't return in
        # ``timeout_sec`` seconds, the process exits with code 124
        # (same convention as /usr/bin/timeout).
        def _timeout_handler(signum, frame):  # noqa: ARG001
            sys.stderr.write(
                f"[memory-sync] {task_name}: TIMEOUT after {timeout_sec}s\n"
            )
            os._exit(124)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_sec)

        try:
            func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"[memory-sync] {task_name}: FAILED: {type(e).__name__}: {e}\n"
            )
            os._exit(1)
        os._exit(0)
    finally:
        # Belt-and-suspenders: never fall through.
        os._exit(1)


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
