"""Per agent Serena roots for one Claude session.

The parent and every sub agent of a Claude session share one MCP session on the Serena router, so the router alone
cannot tell them apart. PreToolUse records the worktree root each agent activates, keyed by session id and agent id
(empty for the parent), and puts that root on the agent's later Serena calls, which the router routes by.
"""

from __future__ import annotations

import subprocess
from typing import Any

from redis import RedisError

from hooks import _redis
from hooks.serena_router.binding import ROOT_ARG, BindingError, resolve

PREFIX = "mcp__serena__"
ACTIVATE = f"{PREFIX}activate_project"
TTL_SECONDS = 7 * 24 * 3600


def _key(session_id: str) -> str:
    return _redis.redis_key("serena_root", session_id)


def record(session_id: str, agent_id: str, project: str) -> None:
    """Store the checkout root an agent activated under its own agent id; a path outside a checkout is not stored."""
    try:
        root, _kind = resolve(project)
    except (BindingError, OSError, subprocess.SubprocessError):
        return
    client = _redis.get_redis()
    if client is None:
        return
    try:
        client.hset(_key(session_id), agent_id, str(root))
        client.expire(_key(session_id), TTL_SECONDS)
    except RedisError:
        return


def root_for(session_id: str, agent_id: str) -> str:
    """The root an agent activated, else its parent's, because a sub agent works in its parent's worktree."""
    client = _redis.get_redis()
    if client is None:
        return ""
    try:
        roots = client.hgetall(_key(session_id))
    except RedisError:
        return ""
    return roots.get(agent_id) or roots.get("") or ""


def rewrite(payload: dict[str, Any]) -> dict[str, str] | None:
    """Record an activation, or return the input patch that carries the calling agent's root on a Serena call."""
    tool_name = str(payload.get("tool_name"))
    session_id = payload.get("session_id")
    if not session_id or not tool_name.startswith(PREFIX):
        return None
    agent_id = payload.get("agent_id") or ""
    if tool_name == ACTIVATE:
        record(session_id, agent_id, str((payload.get("tool_input") or {}).get("project")))
        return None
    root = root_for(session_id, agent_id)
    return {ROOT_ARG: root} if root else None
