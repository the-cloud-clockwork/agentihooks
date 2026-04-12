---
title: MCP Tools
nav_order: 5
has_children: true
permalink: /docs/mcp-tools/
---

# MCP Tools

The AgentiHooks MCP server exposes tools across **7 categories**. The server is started by `python -m hooks.mcp` and registered automatically during `agentihooks init`.

## Categories

| Category | Description |
|----------|-------------|
| [AWS](aws.md) | Profile listing, account ID lookup, account discovery |
| [Email](email.md) | SMTP send with plain text / HTML / markdown options |
| [Storage](storage.md) | S3 upload |
| [Database](database.md) | DynamoDB put, PostgreSQL execute |
| [Compute](compute.md) | AWS Lambda invocation (sync + async) |
| [Observability](observability.md) | Session log diagnostics, container log tailing |
| [Utilities](utilities.md) | Markdown writer, env vars, tool listing |

---

## Filtering categories

By default, all categories load. Use `MCP_CATEGORIES` to restrict:

```bash
MCP_CATEGORIES=aws,utilities python -m hooks.mcp
```

Valid values (comma-separated):

```
aws, email, storage, database,
compute, observability, utilities
```

Setting `MCP_CATEGORIES=all` (the default) loads everything.

---

## Discovering available tools

At runtime, call `hooks_list_tools()` to see exactly which tools are active:

```
hooks_list_tools()
```

Returns: `total_tools`, `available_categories`, and a per-category tool list.
