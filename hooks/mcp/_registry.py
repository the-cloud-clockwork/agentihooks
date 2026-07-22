"""Category module registry — single source of truth for MCP tool categories."""

CATEGORY_MODULES = {
    "channels": "hooks.mcp.channels",
    "enforcement": "hooks.mcp.enforcement",
    "agent_pool": "hooks.mcp.agent_pool",
}

ALL_CATEGORIES = list(CATEGORY_MODULES.keys())
