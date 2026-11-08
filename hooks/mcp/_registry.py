"""Category module registry — single source of truth for MCP tool categories."""

CATEGORY_MODULES = {
    "channels": "hooks.mcp.channels",
    "enforcement": "hooks.mcp.enforcement",
    "conditions": "hooks.mcp.conditions",
    "scheduler": "hooks.mcp.scheduler",
    "recall": "hooks.mcp.recall",
    "sessions": "hooks.mcp.sessions",
    "culture": "hooks.mcp.culture",
    "agents": "hooks.mcp.agents",
    "plans": "hooks.mcp.plans",
}

ALL_CATEGORIES = list(CATEGORY_MODULES.keys())
