"""Category module registry — single source of truth for MCP tool categories."""

CATEGORY_MODULES = {
    "channels": "hooks.mcp.channels",
    "enforcement": "hooks.mcp.enforcement",
    "conditions": "hooks.mcp.conditions",
    "scheduler": "hooks.mcp.scheduler",
    "recall": "hooks.mcp.recall",
    "sessions": "hooks.mcp.sessions",
}

ALL_CATEGORIES = list(CATEGORY_MODULES.keys())
