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


def build_server(categories=None, name="hooks-utils"):
    """Build an MCP server with the requested tool categories.

    Args:
        categories: List of category names to load, or None to resolve
                    from MCP_CATEGORIES env var (default: all).
        name: Server name passed to FastMCP.

    Returns:
        Configured FastMCP instance ready to .run().
    """
    mcp = FastMCP(name)

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
            f"[hooks-utils] WARNING: ignoring unknown MCP categories {unknown}; "
            f"valid categories: {sorted(CATEGORY_MODULES)}",
            file=sys.stderr,
        )
    if not mcp._tool_manager._tools:
        print(
            "[hooks-utils] WARNING: MCP server started with ZERO tools "
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
