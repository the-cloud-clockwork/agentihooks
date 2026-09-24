---
title: MCP Tools
nav_order: 5
has_children: true
---

# MCP Tools

The AgentiHooks MCP server (`hooks-utils`) exposes tools across **2 categories**. The server runs `python -m hooks.mcp` and is registered automatically during `agentihooks init`.

## Categories

| Category | Tools |
|----------|-------|
| **Channels** | `channel_publish`, `channel_list`, `channel_acknowledge`, `channel_clear`, `brain_refresh`, `brain_status` — fleet-command broadcast + brain adapter |
| **Conditions** | `condition_set`, `condition_clear` (only in a turn whose typed prompt asks for it), `condition_list`, `condition_show` — see [Conditions](../hooks/conditions.md#creating-conditions-from-a-session) |
| **Enforcement** | `enforcement_set`, `enforcement_list`, `enforcement_clear` — doctrine reminder banners injected at PreToolUse; `enforcement_set(matcher=...)` limits one to matching tool calls; `local=true` on set/list/clear scopes it to the session's repository |

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

Setting `MCP_CATEGORIES=all` (the default) loads every category.

An unknown category is skipped with a warning on stderr; if every requested category is unknown the server starts with zero tools and warns loudly.

## Transport

stdio by default: Claude Code spawns one server process per session. Where a
policy filters stdio MCP servers out of the client, `MCP_TRANSPORT` switches
`hooks-utils` to `sse` or `streamable-http` and `agentihooks init` runs it as a
daemon instead. See [MCP Transport]({{ site.baseurl }}/hooks/mcp-transport/).

Two consequences of one process serving every session:

- `channel_acknowledge` needs an explicit `session_id`, because no environment
  lookup can identify the caller. SessionStart names it for each session.
- `MCP_CATEGORIES` becomes machine-wide. Per-profile tool subsetting is
  stdio-only, since a url entry in `~/.claude.json` carries no `env` block.
