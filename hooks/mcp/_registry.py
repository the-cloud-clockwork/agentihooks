"""Category module registry — single source of truth for MCP tool categories."""

CATEGORY_MODULES = {
    "aws": "hooks.mcp.aws",
    "email": "hooks.mcp.email",
    "messaging": "hooks.mcp.messaging",
    "storage": "hooks.mcp.storage",
    "database": "hooks.mcp.database",
    "compute": "hooks.mcp.compute",
    "observability": "hooks.mcp.observability",
    "utilities": "hooks.mcp.utilities",
}

ALL_CATEGORIES = list(CATEGORY_MODULES.keys())
