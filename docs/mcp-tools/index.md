---
title: MCP Tools
nav_order: 5
has_children: true
---

# MCP Tools

The AgentiHooks MCP server (`hooks-utils`) exposes tools across **2 categories**. The server is started by `python -m hooks.mcp` and registered automatically during `agentihooks init`.

## Categories

| Category | Tools |
|----------|-------|
| **Channels** | `channel_publish`, `channel_list`, `channel_acknowledge`, `channel_clear`, `brain_refresh`, `brain_status` — fleet-command broadcast + brain adapter |
| **Enforcement** | `enforcement_set`, `enforcement_list`, `enforcement_clear` — doctrine reminder banners injected at PreToolUse |

> Earlier releases shipped generic cloud-utility categories (aws, email, storage, database, compute, observability, utilities). These were removed; only the two agentihooks-native categories above ship now.

---

## Filtering categories

By default, all categories load. Use `MCP_CATEGORIES` to restrict:

```bash
MCP_CATEGORIES=channels python -m hooks.mcp
```

Valid values (comma-separated):

```
channels, enforcement
```

Setting `MCP_CATEGORIES=all` (the default) loads both.

An unknown category is skipped with a warning on stderr; if every requested category is unknown the server starts with zero tools and warns loudly.
