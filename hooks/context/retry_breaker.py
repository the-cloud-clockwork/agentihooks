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

# In-process fallback when Redis is unavailable
# Structure: {session_id: {op_key: {count, last_error_key, last_error_text, last_input}}}
_memory_state: dict[str, dict[str, dict]] = {}

# Prefixes to skip when extracting the base command from a Bash invocation
_BASH_SKIP_TOKENS = frozenset({"sudo", "env", "cd", "nohup", "time", "nice"})

# Tools that use subcommand-level grouping (base:sub or base:sub1:sub2).
# Without this, `terraform plan` and `terraform apply` would share a counter.
_SUBCOMMAND_TOOLS: dict[str, int] = {
    # DevOps / IaC
    "terraform": 1,       # terraform plan, terraform apply, terraform destroy
    "tofu": 1,            # OpenTofu (same subcommands as terraform)
    "pulumi": 1,          # pulumi up, pulumi preview, pulumi destroy
    # Kubernetes
    "kubectl": 1,         # kubectl apply, kubectl get, kubectl delete
    "k": 1,               # kubectl alias
    "helm": 1,            # helm install, helm upgrade, helm rollback
    "kustomize": 1,       # kustomize build, kustomize edit
    # GitOps / CD
    "argocd": 2,          # argocd app sync, argocd app get
    "flux": 1,            # flux reconcile, flux get
    # Cloud CLIs
    "aws": 2,             # aws s3 cp, aws ec2 describe-instances
    "gcloud": 1,          # gcloud compute instances list
    "az": 1,              # az vm list, az group create
    # Containers
    "docker": 1,          # docker build, docker run, docker push
    "podman": 1,          # podman build, podman run
    "docker-compose": 1,  # docker-compose up, docker-compose down
}

# Regex to strip variable content from error text for fingerprinting
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


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


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
            # Strip path prefix (e.g., /usr/bin/python -> python)
            base = p.rsplit("/", 1)[-1]
            base_idx = i
            break

        if not base:
            return "bash:unknown"

        # Check if this tool uses subcommand-level grouping
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
    return tool_name.lower()


def _compute_error_key(error_text: str) -> str:
    """Normalize error text into a stable fingerprint.

    Strips variable content (numbers, hex, paths, timestamps) so that
    similar errors from successive retries produce the same key.
    """
    normalized = _STRIP_RE.sub("", error_text.lower()).strip()
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:80]


# ---------------------------------------------------------------------------
# State management (Redis + memory fallback)
# ---------------------------------------------------------------------------


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

    # Memory fallback
    return _memory_state.get(session_id, {}).get(op_key, _default_state())


def _set_state(session_id: str, op_key: str, state: dict) -> None:
    from hooks.config import RETRY_BREAKER_TTL

    r = get_redis()
    if r is not None:
        try:
            hk = _redis_hash_key(session_id)
            r.hset(hk, op_key, json.dumps(state, separators=(",", ":")))
            r.expire(hk, RETRY_BREAKER_TTL)
        except Exception as e:
            log("retry_breaker: Redis write error", {"error": str(e)})

    # Always update memory fallback (keeps state consistent if Redis drops mid-session)
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


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def _inject_breaker_message(tool_name: str, tool_input: dict, error_text: str, count: int) -> None:
    """Print circuit breaker instructions to stdout (Claude sees this)."""
    from hooks.common import inject_banner
    from hooks.tool_memory import _extract_input_summary

    input_summary = _extract_input_summary(tool_input)

    inject_banner(
        f"CIRCUIT BREAKER: Repeated failure detected ({count}x) — stop retrying",
        f"""You have failed at the same operation {count} times in a row.
STOP retrying this approach. Instead, do the following:

1. Launch 2 error-researcher agents IN PARALLEL (single message, 2 Agent tool calls):
   Agent(description="research error", subagent_type="error-researcher", model="haiku",
         prompt="<error context below>")
   - Agent 1: search for the exact error message
   - Agent 2: search for the tool/command + common causes

2. Wait for both agents to return results.

3. Apply the findings to try a DIFFERENT approach.

If error-researcher agent is unavailable, use WebSearch directly.

ERROR CONTEXT:
  Tool: {tool_name}
  Input: {input_summary}
  Error: {error_text[:200]}
  Consecutive failures: {count}

DO NOT retry the same command. Research first, then try a new approach.""",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def on_post_tool_result(payload: dict) -> None:
    """PostToolUse handler: track failures, inject research instructions at threshold."""
    from hooks.config import RETRY_BREAKER_MAX
    from hooks.tool_memory import _is_error

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})
    tool_result = payload.get("tool_response") or payload.get("tool_result")
    session_id = payload.get("session_id", "")

    if not session_id or tool_result is None:
        return

    op_key = _compute_operation_key(tool_name, tool_input)

    # Detect error (strict mode for MCP tools to avoid false positives)
    is_mcp = tool_name.startswith("mcp__")
    detected, error_text = _is_error(tool_result, strict=is_mcp)

    if not detected:
        # Success — reset counter for this operation
        state = _get_state(session_id, op_key)
        if state["count"] > 0:
            _reset_state(session_id, op_key)
            log("retry_breaker: counter reset on success", {"op_key": op_key})
        return

    # Error detected — compute fingerprint and update counter
    error_key = _compute_error_key(error_text)
    state = _get_state(session_id, op_key)

    if state["last_error_key"] and state["last_error_key"] == error_key:
        # Same error type — increment
        state["count"] += 1
    else:
        # Different error type — reset to 1
        state["count"] = 1

    state["last_error_key"] = error_key
    state["last_error_text"] = error_text[:200]
    state["last_input"] = str(tool_input)[:150]
    _set_state(session_id, op_key, state)

    log(
        "retry_breaker: failure recorded",
        {"op_key": op_key, "count": state["count"], "error_key": error_key[:40]},
    )

    # Trip the breaker at threshold
    if state["count"] >= RETRY_BREAKER_MAX:
        _inject_breaker_message(tool_name, tool_input, error_text, state["count"])


def check_hard_block(payload: dict) -> None:
    """PreToolUse handler: raise BlockAction if consecutive failures exceed hard max.

    Should be called from on_pre_tool_use. Raises BlockAction to prevent
    the tool from executing.
    """
    from hooks.config import RETRY_BREAKER_HARD_MAX
    from hooks.hook_manager import BlockAction

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})
    session_id = payload.get("session_id", "")

    if not session_id:
        return

    op_key = _compute_operation_key(tool_name, tool_input)
    state = _get_state(session_id, op_key)

    if state["count"] >= RETRY_BREAKER_HARD_MAX:
        error_text = state.get("last_error_text", "unknown error")
        raise BlockAction(
            f"BLOCKED: Circuit breaker hard stop. "
            f"This operation ({op_key}) has failed {state['count']} times consecutively. "
            f"Launch error-researcher agents for web research before retrying. "
            f"If unavailable, use WebSearch directly. "
            f"Last error: {error_text[:100]}"
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
