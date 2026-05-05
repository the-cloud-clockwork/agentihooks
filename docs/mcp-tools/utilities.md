---
title: Utilities
nav_order: 13
parent: MCP Tools
---

# Utilities Tools
{: .no_toc }

The Utilities category provides general-purpose tools for markdown writing, environment variable inspection, and tool discovery.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `write_markdown()` | Write a markdown file |
| `get_env()` | Get environment variables with optional filtering |
| `hooks_list_tools()` | List all available MCP tools grouped by category |

---

## Tool reference

### `write_markdown`

```python
write_markdown(
    filepath: str,
    content: str
) -> str
```

Writes a markdown file.

{: .important }
**Path restrictions:** `filepath` must have a `.md` extension and must be under either `$AGENTIHOOKS_HOME/package` or `/tmp`. Writes outside these paths are rejected.

**Returns:** JSON with `filepath`, `bytes_written`

---

### `get_env`

```python
get_env(filter: str = "") -> str
```

Returns environment variables. When `filter` is provided, only variables whose names contain the filter string (case-insensitive) are returned.

```python
# All env vars
get_env()

# Only AWS-related vars
get_env(filter="AWS")

# Only SMTP vars
get_env(filter="SMTP")
```

**Returns:** JSON with `filter`, `count`, `variables` (dict)

{: .note }
Secret values are not redacted by this tool. Use it for diagnostics but avoid logging the output in untrusted contexts.

---

### `hooks_list_tools`

```python
hooks_list_tools() -> str
```

Introspects the running MCP server and returns all registered tools grouped by category. Useful for agents to discover what capabilities are available at runtime without relying on static documentation.

**Returns:** JSON with `total_tools`, `available_categories`, `categories` (dict mapping category name → list of active tool names)

---

## Notes

### `write_markdown` use cases

This tool is designed for agents that generate documentation or reports as part of their task. The path restrictions ensure generated files land in controlled locations:
- `/tmp/<session_id>/` — temporary session artifacts
- `$AGENTIHOOKS_HOME/package/` — persistent package-level files
