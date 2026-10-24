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
| **Recall** | `recall_search`, `recall_get`, `recall_timeline`, `recall_sources` — read only recall of earlier ledgers and swarms; see [Recall](#recall) |

> Earlier releases shipped generic cloud-utility categories (aws, email, storage, database, compute, observability, utilities). These were removed; only the agentihooks-native categories above ship now. Releases before 2.14 registered the server as `hooks-utils`; `agentihooks init` replaces that entry.

---

## Recall

The recall tools read the recall archive (`$AGENTIHOOKS_HOME/recall/recall.sqlite3`), which holds the tasks, phases, comments, chat, questions and follow ups of every indexed ledger. Use them when the operator says remember, yesterday, or names earlier work, instead of reading ledger files. They never write: a missing archive is an error that names `agentihooks recall reindex --all`.

| Tool | Returns |
|------|---------|
| `recall_search(query, scope, kinds, author, seat, period, limit)` | `hits`: ranked items, each with its item key, kind, ref, title, author, time, snippet, archived flag and parent chain. A task id, ref or pull request number in the query comes first |
| `recall_get(ref, scope)` | `item`: one item in full, all of its text, its parent chain and its comments and answers, replies nested under each |
| `recall_timeline(scope, kinds, period, limit)` | `entries`: items oldest first |
| `recall_sources()` | `sources`: each indexed ledger or swarm with its record count, item count, archived item count, last indexed time in epoch milliseconds (the last sync that changed it) and whether its slug sits in the ledger bin; `swarms`: each swarm with its sources and their summed counts, latest indexed time, and binned when every source is binned |

- `scope` empty means the caller's own swarm, read from the session identity; outside a swarm it means every source. A ledger or swarm slug narrows it, and `all` crosses swarms.
- `kinds` is a comma separated list of record kinds such as `task,comment`.
- `period` is `SINCE`, `SINCE..UNTIL` or `..UNTIL`, each an ISO time, epoch milliseconds or a span back from now such as `1d` or `3h`.
- `ref` in `recall_get` is an item key from a search hit (`<ledger>/tasks/<id>`) or a ref inside the scope (`tasks/<id>`). A ref that names items in several ledgers is refused with the item keys to choose from.
- Every result is JSON: `{"success": true, ...}`, or `{"success": false, "error": "..."}`.

The same calls run in a shell as `agentihooks recall search|get|timeline|sources`; see the [CLI reference](../reference/cli-commands.md#agentihooks-recall).

---

## Filtering categories

By default, all categories load. Use `MCP_CATEGORIES` to restrict:

```bash
MCP_CATEGORIES=channels python -m hooks.mcp
```

Valid values (comma-separated):

```
channels, enforcement, conditions, scheduler, recall
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
