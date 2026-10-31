"""Retry Circuit Breaker — stops blind retries after consecutive failures.

When the same operation fails RETRY_BREAKER_MAX times in a row, injects
instructions telling Claude to launch error-researcher agents for web
search before retrying. A hard block (BlockAction) activates at
RETRY_BREAKER_HARD_MAX.

State tracked per session via Redis (with in-memory fallback).

Redis keys:
    agenticore:retry_breaker:{session_id} — Hash of op_key → JSON state

Public API:
    on_post_tool_result(payload)   — called from on_post_tool_use
    check_hard_block(payload)      — called from on_pre_tool_use
    clear_session_state(session_id) — called from on_session_end
"""

import json
import re

from hooks._redis import get_redis, redis_key
from hooks.common import log

_memory_state: dict[str, dict[str, dict]] = {}

_BASH_SKIP_TOKENS = frozenset({"sudo", "env", "cd", "nohup", "time", "nice"})

_SUBCOMMAND_TOOLS: dict[str, int] = {
    "terraform": 1,
    "tofu": 1,
    "pulumi": 1,
    "kubectl": 1,
    "k": 1,
    "helm": 1,
    "kustomize": 1,
    "argocd": 2,
    "flux": 1,
    "aws": 2,
    "gcloud": 1,
    "az": 1,
    "docker": 1,
    "podman": 1,
    "docker-compose": 1,
}

_STRIP_RE = re.compile(
    r"""
      0x[0-9a-fA-F]+        # hex addresses
    | \b\d{4}-\d{2}-\d{2}   # dates
    | \b\d{2}:\d{2}:\d{2}   # times
    | /[\w./\-]+             # file paths
    | \b\d+\b               # bare numbers
    """,
    re.VERBOSE,
)

_SHELL_EXIT_RE = re.compile(r"^(?:Exit code: |Process exited with code )(-?\d+)$", re.MULTILINE)


def _compute_operation_key(tool_name: str, tool_input: dict) -> str:
    """Fingerprint the operation being attempted.

    Bash commands are grouped by base command (npm, pip, cargo, etc.).
    DevOps tools (terraform, kubectl, aws, argocd, etc.) use subcommand-level
    grouping so that e.g. ``terraform plan`` and ``terraform apply`` are
    tracked as separate operations.
    Other tools use the tool name directly.
    """
    if tool_name == "Bash":
        command = (tool_input or {}).get("command", "")
        parts = command.strip().split()
        base = ""
        base_idx = -1
        for i, p in enumerate(parts):
            if p in _BASH_SKIP_TOKENS or "=" in p:
                continue
            if p == "&&":
                break
            base = p.rsplit("/", 1)[-1]
            base_idx = i
            break

        if not base:
            return "bash:unknown"

        depth = _SUBCOMMAND_TOOLS.get(base, 0)
        if depth > 0:
            subs = []
            remaining = parts[base_idx + 1 :]
            for tok in remaining:
                if tok.startswith("-") or "=" in tok or tok == "&&":
                    break
                subs.append(tok)
                if len(subs) >= depth:
                    break
            if subs:
                return f"bash:{base}:{':'.join(subs)}"

        return f"bash:{base}"
    path = (tool_input or {}).get("file_path") or (tool_input or {}).get("notebook_path")
    if path:
        return f"{tool_name.lower()}:{path}"
    return tool_name.lower()


def _payload_operation_key(payload: dict) -> str:
    """Operation key scoped to the calling subagent, so siblings never share a counter."""
    op_key = _compute_operation_key(payload.get("tool_name", "unknown"), payload.get("tool_input", {}))
    agent_id = payload.get("agent_id")
    return f"{agent_id}/{op_key}" if agent_id else op_key


def _compute_error_key(error_text: str) -> str:
    """Normalize error text into a stable fingerprint.

    Strips variable content (numbers, hex, paths, timestamps) so that
    similar errors from successive retries produce the same key.
    """
    normalized = _STRIP_RE.sub("", error_text.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:80]


def _redis_hash_key(session_id: str) -> str:
    return redis_key("retry_breaker", session_id)


def _default_state() -> dict:
    return {"count": 0, "last_error_key": "", "last_error_text": "", "last_input": ""}


def _get_state(session_id: str, op_key: str) -> dict:
    r = get_redis()
    if r is not None:
        try:
            raw = r.hget(_redis_hash_key(session_id), op_key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            log("retry_breaker: Redis read error", {"error": str(e)})

    return _memory_state.get(session_id, {}).get(op_key, _default_state())


def _set_state(session_id: str, op_key: str, state: dict) -> None:
    """Store breaker state in Redis and always in memory, so a Redis drop mid-session keeps it consistent."""
    from hooks.config import RETRY_BREAKER_TTL

    r = get_redis()
    if r is not None:
        try:
            hk = _redis_hash_key(session_id)
            r.hset(hk, op_key, json.dumps(state, separators=(",", ":")))
            r.expire(hk, RETRY_BREAKER_TTL)
        except Exception as e:
            log("retry_breaker: Redis write error", {"error": str(e)})

    _memory_state.setdefault(session_id, {})[op_key] = state


def _reset_state(session_id: str, op_key: str) -> None:
    r = get_redis()
    if r is not None:
        try:
            r.hdel(_redis_hash_key(session_id), op_key)
        except Exception:
            pass

    session_map = _memory_state.get(session_id)
    if session_map:
        session_map.pop(op_key, None)


def _inject_breaker_message(tool_name: str, tool_input: dict, error_text: str, count: int) -> None:
    """Print circuit breaker instructions to stdout (Claude sees this) AND stderr."""
    import sys

    from hooks.common import inject_banner
    from hooks.config import RETRY_BREAKER_HARD_MAX
    from hooks.tool_memory import _extract_input_summary

    input_summary = _extract_input_summary(tool_input)
    remaining = RETRY_BREAKER_HARD_MAX - count

    directive = f"""STOP. You have failed the SAME operation {count} times consecutively.
Hard block activates at {RETRY_BREAKER_HARD_MAX} failures — you have {remaining} attempts left.

This is a HARD DIRECTIVE from the operator's retry-breaker rule:

1. DO NOT retry the same command. Not with minor tweaks. Not "one more time".
   Same approach = same failure. Stop.

2. Launch TWO error-researcher agents in ONE message (both Agent calls in a single tool-use block):

   Agent(description="error exact text",
         subagent_type="error-researcher",
         model="haiku",
         prompt="Search the web for exact error: '<paste exact error>'. Report top 3 causes and fixes.")

   Agent(description="tool + root causes",
         subagent_type="error-researcher",
         model="haiku",
         prompt="Search for '<tool_name> <verb>' common failure modes and workarounds. Report findings.")

3. Read BOTH agent reports. Pick a DIFFERENT approach based on their findings.

4. Retry with the new approach. Do not invoke the original command again until step 3 is done.

ERROR CONTEXT:
  Tool: {tool_name}
  Input: {input_summary}
  Error: {error_text[:200]}
  Consecutive failures: {count}/{RETRY_BREAKER_HARD_MAX}

Retry-breaker is not a suggestion. It is operator policy."""

    inject_banner(
        f"CIRCUIT BREAKER TRIPPED ({count}x) — launch error-researcher agents",
        directive,
    )
    print(
        f"[retry-breaker] FAILURE #{count} on {tool_name}:{_compute_operation_key(tool_name, tool_input)} — launch 2 error-researcher agents NOW",
        file=sys.stderr,
        flush=True,
    )


def _failure_text(payload: dict, tool_result) -> str | None:
    """The failure text when the call failed by its error field or exit status, else None."""
    from hooks.tool_memory import _is_error

    if payload.get("error"):
        return str(payload["error"])
    if isinstance(tool_result, str) and payload.get("tool_name") == "Bash":
        header = tool_result.partition("\nOutput:")[0] if "\nOutput:" in tool_result else tool_result.partition("\n")[0]
        match = _SHELL_EXIT_RE.search(header)
        return tool_result if match and int(match.group(1)) != 0 else None
    detected, error_text = _is_error(tool_result, strict=True)
    return error_text if detected else None


def on_post_tool_result(payload: dict) -> None:
    """PostToolUse handler: track failures, inject research instructions at threshold."""
    from hooks.config import RETRY_BREAKER_MAX

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})
    tool_result = payload.get("tool_response") or payload.get("tool_result")
    session_id = payload.get("session_id", "")

    if not session_id or tool_result is None:
        return

    op_key = _payload_operation_key(payload)
    error_text = _failure_text(payload, tool_result)

    if error_text is None:
        state = _get_state(session_id, op_key)
        if state["count"] > 0:
            _reset_state(session_id, op_key)
            log("retry_breaker: counter reset on success", {"op_key": op_key})
        return

    error_key = _compute_error_key(error_text)
    state = _get_state(session_id, op_key)

    if state["last_error_key"] and state["last_error_key"] == error_key:
        state["count"] += 1
    else:
        state["count"] = 1

    state["last_error_key"] = error_key
    state["last_error_text"] = error_text[:200]
    state["last_input"] = str(tool_input)[:150]
    _set_state(session_id, op_key, state)

    log(
        "retry_breaker: failure recorded",
        {"op_key": op_key, "count": state["count"], "error_key": error_key[:40]},
    )

    if state["count"] >= RETRY_BREAKER_MAX:
        _inject_breaker_message(tool_name, tool_input, error_text, state["count"])


def check_hard_block(payload: dict) -> None:
    """PreToolUse handler: raise BlockAction if consecutive failures exceed hard max.

    Should be called from on_pre_tool_use. Raises BlockAction to prevent
    the tool from executing. Half-open: after each block one trial call goes through,
    so a success can reset the counter instead of waiting out the TTL.
    """
    from hooks.config import RETRY_BREAKER_HARD_MAX
    from hooks.hook_manager import BlockAction

    session_id = payload.get("session_id", "")

    if not session_id:
        return

    op_key = _payload_operation_key(payload)
    state = _get_state(session_id, op_key)

    if state["count"] >= RETRY_BREAKER_HARD_MAX:
        if state.get("blocked"):
            state["blocked"] = False
            _set_state(session_id, op_key, state)
            return
        state["blocked"] = True
        _set_state(session_id, op_key, state)
        error_text = state.get("last_error_text", "unknown error")
        raise BlockAction(
            f"BLOCKED: Circuit breaker HARD STOP.\n"
            f"Operation '{op_key}' has failed {state['count']} times consecutively.\n"
            f"\n"
            f"You MUST now launch 2 error-researcher agents (single message, 2 Agent tool calls):\n"
            f"  Agent(subagent_type='error-researcher', model='haiku', prompt='<exact error>')\n"
            f"  Agent(subagent_type='error-researcher', model='haiku', prompt='<tool + causes>')\n"
            f"\n"
            f"Then read BOTH reports and pick a DIFFERENT approach.\n"
            f"The next call to this operation runs as a single trial; another failure blocks again.\n"
            f"If error-researcher is unavailable, use WebSearch directly.\n"
            f"\n"
            f"Last error: {error_text[:150]}"
        )


def clear_session_state(session_id: str) -> None:
    """Delete all breaker state for a session. Called from on_session_end."""
    r = get_redis()
    if r is not None:
        try:
            r.delete(_redis_hash_key(session_id))
        except Exception as e:
            log("retry_breaker: Redis delete error", {"error": str(e)})

    _memory_state.pop(session_id, None)
