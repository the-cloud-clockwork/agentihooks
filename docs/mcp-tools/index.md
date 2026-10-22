---
title: MCP Tools
nav_order: 5
has_children: true
---

# MCP Tools

The AgentiHooks MCP server (`agentihooks`) serves Claude Code and Codex and exposes tools across **4 categories**. The server runs `python -m hooks.mcp` and is registered automatically during `agentihooks init`.

See [Server Startup](startup.md) for deferred stdio loading, first call behavior,
configuration examples and troubleshooting.
Its [measured idle memory](startup.md#measured-idle-memory) section records the
before and after cohorts, read only inspection and limits of the comparison.

## Categories

| Category | Tools |
|----------|-------|
| **Channels** | `channel_publish`, `channel_list`, `channel_acknowledge`, `channel_clear`, `brain_refresh`, `brain_status` — fleet-command broadcast + brain adapter |
| **Conditions** | `condition_set`, `condition_clear` (only on the operator's request: a typed prompt this turn, his comment on the agent's ledger task, or the master's relay of it), `condition_list`, `condition_show` — see [Conditions](../hooks/conditions.md#creating-conditions-from-a-session) |
| **Enforcement** | `enforcement_set`, `enforcement_list`, `enforcement_clear` — doctrine banners injected at PreToolUse; `type="rule"` with an absolute `path` injects the complete current file; `matcher` limits one to matching tool calls; `local=true` scopes it to the session's repository |
| **Scheduler** | `timer_add`, `timer_list`, `timer_cancel`, `stopwatch_start`, `stopwatch_stop`, `stopwatch_show`, `cron_add`, `cron_list`, `cron_remove` — deliver a message once, when a stopwatch crosses its limit, or on a cron expression; `target` is `fleet` (a broadcast), `swarm` (one inbox item per live agent of the swarm in `name`), `seat` or `session` (an inbox item to `name`). On a shared network server a caller passes `agent_name` or `session_id` so its schedule has an owner and a swarm firing skips it. The same commands run as `agentihooks timer`, `agentihooks stopwatch` and `agentihooks cron` |

> Earlier releases shipped generic cloud-utility categories (aws, email, storage, database, compute, observability, utilities). These were removed; only the agentihooks-native categories above ship now. Releases before 2.14 registered the server as `hooks-utils`; `agentihooks init` replaces that entry.

---

## Filtering categories

By default, all categories load. Use `MCP_CATEGORIES` to restrict:

```bash
MCP_CATEGORIES=channels python -m hooks.mcp
```

Valid values (comma-separated):

```
channels, enforcement, conditions, scheduler
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
