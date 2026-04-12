"""Category module registry — single source of truth for MCP tool categories."""

CATEGORY_MODULES = {
    "aws": "hooks.mcp.aws",
    "email": "hooks.mcp.email",
    "storage": "hooks.mcp.storage",
    "database": "hooks.mcp.database",
    "compute": "hooks.mcp.compute",
    "observability": "hooks.mcp.observability",
    "channels": "hooks.mcp.channels",
    "profiles": "hooks.mcp.profiles",
    "utilities": "hooks.mcp.utilities",
}

ALL_CATEGORIES = list(CATEGORY_MODULES.keys())
