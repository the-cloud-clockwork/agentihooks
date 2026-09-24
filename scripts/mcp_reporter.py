"""MCP surface area reporter — analyzes MCP server token cost.

Reads the MCP registry of whichever agent CLI invoked the hook — claude's
~/.claude.json plus project .mcp.json, codex's config.toml [mcp_servers], or
copilot's mcp-config.json — enumerates tools per server, and estimates context
token overhead. Reading claude's registry inside a codex session reports
servers that session cannot see.
"""

import json
from pathlib import Path
from typing import Optional


def _codex_mcp_configs() -> dict[str, dict]:
    from hooks.targets import codex_home

    config = codex_home() / "config.toml"
    if not config.exists():
        return {}
    try:
        import tomllib

        table = tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {})
    except (OSError, ValueError):
        return {}
    return {name: {"source": "user", "config": spec} for name, spec in table.items() if isinstance(spec, dict)}


def _copilot_mcp_configs() -> dict[str, dict]:
    from hooks.targets import copilot_home

    config = copilot_home() / "mcp-config.json"
    if not config.exists():
        return {}
    try:
        table = json.loads(config.read_text(encoding="utf-8")).get("mcpServers", {})
    except (json.JSONDecodeError, OSError):
        return {}
    return {name: {"source": "user", "config": spec} for name, spec in table.items() if isinstance(spec, dict)}


def load_all_mcp_configs(project_path: Optional[str] = None) -> dict[str, dict]:
    """Load MCP server configs for the invoking target.

    Returns:
        {server_name: {"source": "user"|"project", "config": {...}, "tool_count": int}}
    """
    from hooks.targets import current_target

    target = current_target()
    if target == "codex":
        return _codex_mcp_configs()
    if target == "copilot":
        return _copilot_mcp_configs()

    servers: dict[str, dict] = {}

    # User scope: ~/.claude.json
    user_mcp = Path.home() / ".claude.json"
    if user_mcp.exists():
        try:
            data = json.loads(user_mcp.read_text(encoding="utf-8"))
            for name, config in data.get("mcpServers", {}).items():
                servers[name] = {"source": "user", "config": config}
        except (json.JSONDecodeError, OSError):
            pass

    # Project scope: .mcp.json in CWD or provided path
    project_dir = Path(project_path) if project_path else Path.cwd()
    project_mcp = project_dir / ".mcp.json"
    if project_mcp.exists():
        try:
            data = json.loads(project_mcp.read_text(encoding="utf-8"))
            for name, config in data.get("mcpServers", {}).items():
                servers[name] = {"source": "project", "config": config}
        except (json.JSONDecodeError, OSError):
            pass

    return servers


def count_tools_per_server(servers: dict[str, dict]) -> dict[str, int]:
    """Estimate tool count per server.

    For the agentihooks server, reads from the MCP registry.
    For external servers, uses a default estimate based on server type.
    """
    counts: dict[str, int] = {}

    for name, info in servers.items():
        config = info.get("config", {})

        from scripts.targets._common import LEGACY_MCP_SERVER_NAMES, MCP_SERVER_NAME

        if name in (MCP_SERVER_NAME, *LEGACY_MCP_SERVER_NAMES):
            try:
                import os

                from hooks.mcp._registry import CATEGORY_MODULES

                cats = os.getenv("MCP_CATEGORIES", "all")
                if cats.lower() == "all":
                    # Each category module has ~3-4 tools on average
                    counts[name] = len(CATEGORY_MODULES) * 4
                else:
                    counts[name] = len(cats.split(",")) * 4
            except ImportError:
                counts[name] = 20  # fallback estimate
            continue

        # For stdio/sse servers, we can't introspect — use heuristic
        # Typical MCP server has 5-15 tools
        args = config.get("args", [])
        if any("github" in str(a).lower() for a in args):
            counts[name] = 40  # GitHub MCP is large
        elif any("sonar" in str(a).lower() for a in args):
            counts[name] = 25
        else:
            counts[name] = 10  # conservative default

    return counts


def estimate_schema_tokens(tool_count: int, avg_tokens_per_tool: int = 150) -> int:
    """Estimate total schema tokens for a tool count."""
    return tool_count * avg_tokens_per_tool


def generate_report(servers: dict[str, dict], avg_tokens: int = 150) -> str:
    """Generate a formatted report of MCP servers and their token cost."""
    if not servers:
        return "No MCP servers found."

    counts = count_tools_per_server(servers)
    total_tools = sum(counts.values())
    total_tokens = estimate_schema_tokens(total_tools, avg_tokens)

    lines = [
        "MCP Surface Area Report",
        f"Total: {len(servers)} servers, ~{total_tools} tools, ~{total_tokens:,} schema tokens",
        "",
        f"{'Server':<30} {'Source':>8} {'Tools':>7} {'~Tokens':>9}",
        f"{'─' * 30} {'─' * 8} {'─' * 7} {'─' * 9}",
    ]

    sorted_servers = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    for name, tool_count in sorted_servers:
        source = servers[name].get("source", "?")
        tokens = estimate_schema_tokens(tool_count, avg_tokens)
        lines.append(f"{name:<30} {source:>8} {tool_count:>7} {tokens:>9,}")

    return "\n".join(lines)


def generate_warning(servers: dict[str, dict], threshold: int = 40, avg_tokens: int = 150) -> Optional[str]:
    """Generate a warning string if total tools exceed threshold.

    Returns:
        Warning string with top 3 heaviest servers, or None.
    """
    counts = count_tools_per_server(servers)
    total = sum(counts.values())

    if total <= threshold:
        return None

    top3 = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]
    total_tokens = estimate_schema_tokens(total, avg_tokens)
    details = ", ".join(f"{name} ({n} tools)" for name, n in top3)

    return (
        f"MCP overhead: ~{total} tools (~{total_tokens:,} schema tokens). "
        f"Heaviest: {details}. "
        "Disable unused servers via /mcp to reduce context cost."
    )
