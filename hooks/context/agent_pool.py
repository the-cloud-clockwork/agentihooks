"""Agent pool — directed agent-to-agent messaging over the session registry.

The "pool" is not a new registry: it IS the broadcast session registry
(``~/.agentihooks/active-sessions.json``, managed by ``broadcast.register_session``
/ ``mark_session_closed`` / ``heartbeat_sessions``). This module adds two things
on top of it:

1. A per-session **summary** ("what this agent is doing"), refreshed every N tool
   calls from the session's own transcript tail (no LLM by default), so a peer
   scanning the pool can tell who is working on what.
2. ``call_agent`` — deliver a message to a specific peer by session id. It does
   the SAME two safe things for every peer, live or dormant:

   - **(a) Directed inbox** — a ``target_session`` broadcast the peer reads
     itself, on its next turn (or before its next tool call, via PreToolUse).
     Single-writer, so it can never corrupt the peer's transcript.
   - **(b) Fork-read** — a throwaway ``--resume … --fork-session`` copy answers
     the caller with what the peer is doing. The fork writes a NEW throwaway id
     and leaves the peer's real transcript untouched.

   We NEVER run an un-forked ``--resume``: that would be a second writer on the
   peer's real transcript (two concurrent callers, or a TOCTOU wake within the
   subprocess window, corrupt it) and, under bypassPermissions, an
   identity-laundering action vector. Liveness only labels the result (does the
   peer see the inbox soon), it does not change the mechanism.

The subprocess runs with ``--bare`` (no MCP/hooks/skills/CLAUDE.md) plus a
built-in ``--disallowedTools`` list — it has no tools and answers purely from the
resumed context. ``call_agent`` is a communication primitive, not remote
execution.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hooks.common import log
from hooks.config import (
    AGENT_POOL_CALL_MODEL,
    AGENT_POOL_CALL_TIMEOUT,
    AGENT_POOL_COUNTER_FILE,
    AGENT_POOL_ENABLED,
    AGENT_POOL_IDLE_THRESHOLD,
    AGENT_POOL_SUMMARY_HAIKU,
    AGENT_POOL_SUMMARY_INTERVAL,
)
from hooks.context.broadcast import (
    _file_lock,
    _load_sessions,
    _save_sessions,
    _sessions_path,
    create_broadcast,
    encode_cwd,
    get_active_sessions,
)

_SUMMARY_MAX_LEN = 280

# Tools the fork/resume subprocess may NOT use — it answers from context only,
# never acts. Keeps call_agent a pure communication tool.
_NO_ACTION_TOOLS = "Bash,Write,Edit,NotebookEdit,Task,Agent,WebFetch,WebSearch,Read,Glob,Grep"

_FORK_READ_WRAPPER = (
    "[agent-pool] Another agent in the fleet is trying to coordinate with you and asks:\n\n"
    "{message}\n\n"
    "Answer in 1-3 sentences based ONLY on what THIS session has been doing so far. "
    "Do not take any actions or use any tools — just report your current status."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Per-session summary refresh counter (mirrors enforcement.py)
# ---------------------------------------------------------------------------


def _counter_path() -> Path:
    return Path(AGENT_POOL_COUNTER_FILE).expanduser()


def _load_counters() -> dict:
    p = _counter_path()
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_counters(state: dict) -> None:
    p = _counter_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name per writer — a fixed ".tmp" is shared by every concurrent
    # session and races os.replace (lost writes + FileNotFoundError).
    tmp = p.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(str(tmp), str(p))


def _increment_and_get_count(session_id: str) -> int:
    # Lock the read-modify-write: many sessions increment their own keys in the
    # one shared counter file concurrently; without the lock one clobbers another.
    with _file_lock(_counter_path()):
        state = _load_counters()
        cur = int(state.get(session_id, 0)) + 1
        state[session_id] = cur
        _save_counters(state)
    return cur


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


def _transcript_path(session_id: str, cwd: str) -> Path:
    return Path.home() / ".claude" / "projects" / encode_cwd(cwd) / f"{session_id}.jsonl"


def _extract_text(content) -> str:
    """Flatten a Claude Code message.content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in (None, "text")]
        return " ".join(p for p in parts if p).strip()
    return ""


def _derive_summary_from_transcript(session_id: str, cwd: str) -> str:
    """Cheap, LLM-free summary: last user prompt + last assistant text.

    The transcript JSONL schema is officially internal/unstable, so this is a
    best-effort read guarded to never raise — an empty summary is acceptable.
    """
    path = _transcript_path(session_id, cwd)
    if not path.exists():
        return ""
    last_user = ""
    last_assistant = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = obj.get("type")
                if etype not in ("user", "assistant"):
                    continue
                text = _extract_text(obj.get("message", {}).get("content", ""))
                if not text:
                    continue
                if etype == "user":
                    # Skip tool-result / hook envelopes that aren't real prompts.
                    if text.startswith("<") or text.startswith("[") or "task-notification" in text:
                        continue
                    last_user = text.replace("\n", " ")
                else:
                    last_assistant = text.replace("\n", " ")
    except OSError:
        return ""

    parts = []
    if last_user:
        parts.append(f"task: {last_user}")
    if last_assistant:
        parts.append(f"last: {last_assistant}")
    return " | ".join(parts)[:_SUMMARY_MAX_LEN]


def _derive_summary_haiku(session_id: str, cwd: str) -> str:
    """Optional richer summary via a Haiku call over the transcript tail."""
    base = _derive_summary_from_transcript(session_id, cwd)
    if not base:
        return ""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return base
    prompt = (
        "Summarize what this agent session is currently working on in ONE short "
        f"sentence (max 20 words), from these transcript fragments:\n\n{base}"
    )
    cmd = [
        claude_bin,
        "-p",
        "--model",
        "haiku",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools",
        _NO_ACTION_TOOLS,
        prompt,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        out = (result.stdout or "").strip()
        return out[:_SUMMARY_MAX_LEN] if out else base
    except (OSError, subprocess.SubprocessError):
        return base


# ---------------------------------------------------------------------------
# Summary read/write
# ---------------------------------------------------------------------------


def set_summary(session_id: str, summary: str, sticky: bool | None = None) -> bool:
    """Write a summary onto a session's pool entry. Returns False if unregistered.

    ``sticky`` controls the pin that protects a self-declared summary from being
    overwritten by the auto-derive tick: ``True`` pins it, ``False`` clears the
    pin, ``None`` leaves the pin as-is (the auto-derive path passes ``None`` and
    never reaches a pinned entry because ``maybe_refresh_summary`` skips those).
    """
    with _file_lock(_sessions_path()):
        sessions = _load_sessions()
        entry = sessions.get(session_id)
        if entry is None:
            return False
        entry["summary"] = (summary or "").strip()[:_SUMMARY_MAX_LEN]
        entry["summary_at"] = _now_iso()
        if sticky is True:
            entry["summary_sticky"] = True
        elif sticky is False:
            entry.pop("summary_sticky", None)
        _save_sessions(sessions)
    return True


def maybe_refresh_summary(session_id: str) -> None:
    """PostToolUse hook: every Nth tool call, refresh this session's summary.

    Refreshes on the first tool call and every ``AGENT_POOL_SUMMARY_INTERVAL``-th
    thereafter. A self-declared summary (via ``pool_status``) is preserved — it is
    only overwritten by this auto-derive on a refresh tick, which is intended
    (the transcript is the ground truth of what's actually happening).
    """
    if not AGENT_POOL_ENABLED:
        return
    try:
        count = _increment_and_get_count(session_id)
        if count != 1 and count % max(1, AGENT_POOL_SUMMARY_INTERVAL) != 0:
            return
        sessions = _load_sessions()
        entry = sessions.get(session_id)
        if entry is None:
            return
        if entry.get("summary_sticky"):
            # A self-declared pool_status summary is pinned — never clobber it.
            return
        cwd = entry.get("cwd", "")
        if not cwd:
            return
        if AGENT_POOL_SUMMARY_HAIKU:
            summary = _derive_summary_haiku(session_id, cwd)
        else:
            summary = _derive_summary_from_transcript(session_id, cwd)
        if summary:
            set_summary(session_id, summary)
    except Exception as e:
        log("agent_pool summary refresh failed", {"error": str(e)})


def clear_pool_session(session_id: str) -> None:
    """Drop this session's refresh counter on SessionEnd. The registry entry
    itself is retired by broadcast.mark_session_closed (24h retention)."""
    try:
        state = _load_counters()
        if session_id in state:
            del state[session_id]
            _save_counters(state)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pool listing
# ---------------------------------------------------------------------------


def list_pool(include_self: str = "") -> list[dict]:
    """Return live peers with their summaries — the scan an agent reads before
    deciding whom to call. Excludes ``include_self`` (the caller's session id)."""
    sessions = get_active_sessions(cleanup=True)
    out = []
    for sid, info in sessions.items():
        if sid == include_self:
            continue
        out.append(
            {
                "session_id": sid,
                "cwd": info.get("cwd", ""),
                "model": info.get("model", ""),
                "status": info.get("status", ""),
                "summary": info.get("summary", ""),
                "last_seen": info.get("last_seen", ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def _pid_is_claude(pid: int) -> bool:
    """Best-effort: confirm the pid is actually a claude process (Linux /proc),
    so a recycled pid running something unrelated doesn't read as a live peer.
    If /proc is unavailable (non-Linux), assume True — don't over-reject."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return b"claude" in cmdline


def _pid_alive(pid) -> bool:
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return _pid_is_claude(int(pid))


def _transcript_recent(session_id: str, cwd: str) -> bool:
    if not session_id or not cwd:
        return False
    try:
        mtime = _transcript_path(session_id, cwd).stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < AGENT_POOL_IDLE_THRESHOLD


def is_peer_live(session_id: str, entry: dict) -> bool:
    """A peer is live (→ fork, never resume) if its process is alive OR its
    transcript was written within the idle window. Resume only a peer that is
    genuinely stopped on both signals."""
    if _pid_alive(entry.get("pid")):
        return True
    return _transcript_recent(session_id, entry.get("cwd", ""))


# ---------------------------------------------------------------------------
# call_agent
# ---------------------------------------------------------------------------


def _run_claude(extra_args: list[str], prompt: str, cwd: str) -> tuple[int, str, str]:
    """Spawn a headless claude subprocess. Returns (returncode, result_text, raw_stdout)."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return (127, "", "claude CLI not found in PATH")
    cmd = [
        claude_bin,
        # --bare strips auto-discovery of MCP servers, hooks, skills, and
        # CLAUDE.md for this child. That is the load-bearing safety flag:
        # --disallowedTools does NOT block mcp__* tools in -p mode (upstream
        # claude-code#12863), so without --bare a fork/resume under
        # bypassPermissions could invoke the peer's real MCP surface (litellm
        # mutation, github, notifications, call_agent recursively). --bare leaves
        # the child with no tools at all — it answers purely from the resumed
        # transcript's context. Communication, not remote execution.
        "--bare",
        "-p",
        "--model",
        AGENT_POOL_CALL_MODEL,
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        # Belt-and-suspenders on top of --bare: blocks the built-in tools too.
        "--disallowedTools",
        _NO_ACTION_TOOLS,
        *extra_args,
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=AGENT_POOL_CALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    except OSError as e:
        return (1, "", str(e))

    raw = (proc.stdout or "").strip()
    result_text = raw
    fork_sid = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            result_text = parsed.get("result", raw)
            fork_sid = parsed.get("session_id", "")
    except json.JSONDecodeError:
        pass
    # Stash the (possibly new) session id in the third slot for fork cleanup.
    return (proc.returncode, result_text, fork_sid)


def _cleanup_fork(cwd: str, fork_sid: str, original_sid: str) -> None:
    """Delete a throwaway forked transcript. Never touch the original session."""
    if not fork_sid or fork_sid == original_sid:
        return
    try:
        _transcript_path(fork_sid, cwd).unlink(missing_ok=True)
    except OSError:
        pass


def call_agent(target_session_id: str, message: str, caller_session_id: str = "") -> dict:
    """Deliver a message to a peer by session id, routed by the peer's liveness.

    Returns a dict describing exactly what happened (``mode``, ``delivered``,
    ``their_state``/``reply``), so the caller always knows the effect.
    """
    if not AGENT_POOL_ENABLED:
        return {"success": False, "error": "agent pool disabled"}
    if not target_session_id or not message or not message.strip():
        return {"success": False, "error": "target_session_id and message are required"}

    caller = caller_session_id or os.getenv("CLAUDE_SESSION_ID", "")
    if target_session_id == caller:
        return {"success": False, "error": "cannot call your own session"}

    sessions = _load_sessions()
    entry = sessions.get(target_session_id)
    if entry is None:
        return {"success": False, "error": f"no session {target_session_id[:8]} in the pool"}
    status = entry.get("status", "")
    if status != "alive":
        # superseded / closed / dead entries keep a stale pid whose liveness check
        # would misfire — an inbox nobody polls, framed as success. Reject up front.
        return {
            "success": False,
            "error": f"session {target_session_id[:8]} is not alive (status={status or 'unknown'})",
        }
    cwd = entry.get("cwd", "")
    if not cwd:
        return {"success": False, "error": "target session has no recorded cwd (cannot fork-read)"}

    live = is_peer_live(target_session_id, entry)
    source = f"agent:{caller[:8]}" if caller else "agent:unknown"

    # (1) Deliver via the directed inbox — single-writer, the peer reads it itself
    # on its next turn / before its next tool call. Safe for live and dormant.
    delivered = False
    try:
        msg_id = create_broadcast(
            message.strip(),
            severity="alert",
            source=source,
            target_session=target_session_id,
        )
        delivered = bool(msg_id)
    except Exception as e:
        log("call_agent inbox deliver failed", {"error": str(e)})

    # (2) Read the peer's context via a THROWAWAY --fork-session — NEVER an
    # un-forked --resume. An un-forked resume is a second writer on the peer's
    # real transcript (two concurrent call_agents, or a TOCTOU wake within the
    # subprocess window, corrupt it) and, for a dormant peer, an
    # identity-laundering action vector. Forking writes a new throwaway id and
    # leaves the original untouched; --bare (in _run_claude) leaves it toolless.
    rc, their_state, fork_sid = _run_claude(
        ["--resume", target_session_id, "--fork-session"],
        _FORK_READ_WRAPPER.format(message=message.strip()),
        cwd,
    )
    _cleanup_fork(cwd, fork_sid, target_session_id)

    result = {
        "success": True,
        # Informational only — the mechanism is identical (fork + inbox) either
        # way; liveness tells the caller whether the peer will see the inbox soon.
        "mode": "live" if live else "dormant",
        "delivered": delivered,
        "their_state": their_state if their_state else "",
    }
    if not delivered or (rc != 0 and not their_state):
        result["note"] = (
            f"inbox {'delivered' if delivered else 'FAILED'}; "
            f"fork-read {'ok' if their_state else f'failed rc={rc}'}; "
            f"peer is {'live (sees it on its next turn/tool call)' if live else 'dormant (sees it only if reopened before TTL)'}"
        )
    return result
