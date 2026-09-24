"""Hooks MCP server — modular composition engine.

Usage:
    # All categories (default):
    python -m hooks.mcp

    # Specific categories via env var:
    MCP_CATEGORIES=channels,enforcement python -m hooks.mcp

    # Programmatic:
    from hooks.mcp import build_server
    mcp = build_server(categories=["channels", "enforcement"])
    mcp.run()
"""

import importlib
import os
import sys

from mcp.server.fastmcp import FastMCP

from hooks.common import log
from hooks.mcp._registry import ALL_CATEGORIES, CATEGORY_MODULES
from hooks.mcp._session import set_env_fallback_allowed

# Transport settings live here rather than in hooks/config.py: they are read
# only by the MCP entry point, alongside the MCP_CATEGORIES / ALLOWED_TOOLS
# precedent already in this module.
VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")
# Kept identical to the installer's default so the daemon binds whatever the
# client's url resolves to. Splitting the two invites a bind on 127.0.0.1 against
# a url naming `localhost` — which fails wherever that name resolves to ::1 first.
_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 8642


def _env_flag(key: str) -> bool:
    return os.getenv(key, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"[agentihooks] WARNING: {key}={raw!r} is not an integer; using {default}.",
            file=sys.stderr,
        )
        return default


def resolve_transport() -> str:
    """Resolve MCP_TRANSPORT to a transport name, defaulting to stdio.

    An unrecognised value falls back to stdio with a stderr warning rather than
    crashing — a typo should not take the whole toolbelt offline.
    """
    raw = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if raw not in VALID_TRANSPORTS:
        print(
            f"[agentihooks] WARNING: unknown MCP_TRANSPORT={raw!r}; falling back to stdio. "
            f"Valid values: {', '.join(VALID_TRANSPORTS)}",
            file=sys.stderr,
        )
        return "stdio"
    return raw


def build_server(categories=None, name="agentihooks"):
    """Build an MCP server with the requested tool categories.

    Args:
        categories: List of category names to load, or None to resolve
                    from MCP_CATEGORIES env var (default: all).
        name: Server name passed to FastMCP.

    Returns:
        Configured FastMCP instance ready to .run().
    """
    # Host/port/paths are FastMCP *constructor* settings, not .run() arguments —
    # .run() takes only transport and mount_path. They are inert under stdio, so
    # they are set unconditionally and the transport choice stays in __main__.
    # Under a network transport the process serves every session at once, so an
    # inherited CLAUDE_CODE_SESSION_ID names some unrelated session rather than
    # the caller. Shut the env fallback off before any tool can consult it.
    set_env_fallback_allowed(resolve_transport() == "stdio")

    mcp = FastMCP(
        name,
        host=os.getenv("MCP_HOST", _DEFAULT_HOST),
        port=_env_int("MCP_PORT", _DEFAULT_PORT),
        sse_path=os.getenv("MCP_SSE_PATH", "/sse"),
        streamable_http_path=os.getenv("MCP_STREAMABLE_HTTP_PATH", "/mcp"),
        stateless_http=_env_flag("MCP_STATELESS_HTTP"),
    )

    if categories is None:
        categories = _resolve_categories()

    unknown = [c for c in categories if c not in CATEGORY_MODULES]
    for cat in categories:
        if cat not in CATEGORY_MODULES:
            log("MCP build_server unknown category", {"category": cat})
            continue
        mod = importlib.import_module(CATEGORY_MODULES[cat])
        mod.register(mcp)

    _apply_allowed_tools_filter(mcp)
    _strip_output_schema(mcp)

    # Surface config drift loudly. A stale MCP_CATEGORIES referencing a
    # removed category would otherwise yield a zero-tool server whose only
    # trace is a log file the operator never opens. Warn on stderr, which
    # Claude Code captures for the MCP process.
    if unknown:
        print(
            f"[agentihooks] WARNING: ignoring unknown MCP categories {unknown}; "
            f"valid categories: {sorted(CATEGORY_MODULES)}",
            file=sys.stderr,
        )
    if not mcp._tool_manager._tools:
        print(
            "[agentihooks] WARNING: MCP server started with ZERO tools "
            f"(requested categories: {list(categories)}). Check MCP_CATEGORIES.",
            file=sys.stderr,
        )
    return mcp


def _strip_output_schema(mcp) -> None:
    """Strip outputSchema from all tools — CC versions prior to outputSchema support fail on it."""
    for tool in mcp._tool_manager._tools.values():
        if getattr(tool, "output_schema", None) is not None:
            tool.output_schema = None


def _resolve_categories():
    """Read MCP_CATEGORIES env var and return a list of category names."""
    raw = os.getenv("MCP_CATEGORIES", "").strip()
    if not raw or raw.lower() == "all":
        return list(ALL_CATEGORIES)
    return [c.strip().lower() for c in raw.split(",") if c.strip()]


def _apply_allowed_tools_filter(mcp):
    """Legacy ALLOWED_TOOLS env-var filter for backward compatibility."""
    allowed_raw = os.getenv("ALLOWED_TOOLS", "").strip()
    if not allowed_raw:
        return

    allowed_tools = {t.strip().lower() for t in allowed_raw.split(",") if t.strip()}
    tool_names = list(mcp._tool_manager._tools.keys())

    for tool_name in tool_names:
        if tool_name.lower() not in allowed_tools:
            try:
                mcp._tool_manager.remove_tool(tool_name)
            except Exception as e:
                log("MCP tool filter failed", {"tool": tool_name, "error": str(e)})
