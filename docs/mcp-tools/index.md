---
title: MCP Tools
nav_order: 5
has_children: true
---

# MCP Tools

The AgentiHooks MCP server (`agentihooks`) exposes tools across **3 categories**. The server runs `python -m hooks.mcp` and is registered automatically during `agentihooks init`.

## Categories

| Category | Tools |
|----------|-------|
| **Channels** | `channel_publish`, `channel_list`, `channel_acknowledge`, `channel_clear`, `brain_refresh`, `brain_status` — fleet-command broadcast + brain adapter |
| **Conditions** | `condition_set`, `condition_clear` (only on the operator's request: a typed prompt this turn, his comment on the agent's ledger task, or the master's relay of it), `condition_list`, `condition_show` — see [Conditions](../hooks/conditions.md#creating-conditions-from-a-session) |
| **Enforcement** | `enforcement_set`, `enforcement_list`, `enforcement_clear` — doctrine banners injected at PreToolUse; `type="rule"` with an absolute `path` injects the complete current file; `matcher` limits one to matching tool calls; `local=true` scopes it to the session's repository |

> Earlier releases shipped generic cloud-utility categories (aws, email, storage, database, compute, observability, utilities). These were removed; only the three agentihooks-native categories above ship now. Releases before 2.14 registered the server as `hooks-utils`; `agentihooks init` replaces that entry.

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
`agentihooks` to `sse` or `streamable-http` and `agentihooks init` runs it as a
daemon instead. See [MCP Transport]({{ site.baseurl }}/hooks/mcp-transport/).

Two consequences of one process serving every session:

- `channel_acknowledge` needs an explicit `session_id`, because no environment
  lookup can identify the caller. SessionStart names it for each session.
- `MCP_CATEGORIES` becomes machine-wide. Per-profile tool subsetting is
  stdio-only, since a url entry in `~/.claude.json` carries no `env` block.
