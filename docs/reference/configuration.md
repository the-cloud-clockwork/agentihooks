---
title: Configuration
nav_order: 2
parent: Reference
---

# Configuration Reference
{: .no_toc }

All environment variables recognized by AgentiHooks, grouped by integration. Variables with no default are required for their integration to function.

The generated `~/.agentihooks/.env` contains only AgentiHooks-owned runtime
settings. Harness settings stay in native client configuration, brain settings
stay in `~/.agentibrain/.env`, and integration credentials use
connector-specific companion environment files.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Install & Automation

These variables control how `agentihooks init` installs and configures Claude Code and Codex. Harness-specific variables apply only to their named client. They are read at install time, not at hook runtime.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_CODE_HOME_DIR` | `$HOME` | Home-directory root override. When set, agentihooks targets `$CLAUDE_CODE_HOME_DIR/.claude` for all install operations. Use when `$HOME` differs from where Claude Code stores its config (e.g. shared volumes). |
| `AGENTIHOOKS_CLAUDE_HOME` | `~/.claude` | Legacy: direct path to the `.claude` directory. `CLAUDE_CODE_HOME_DIR` takes priority if both are set. |
| `AGENTIHOOKS_PROFILE` | `default` | Profile to use when `--profile` is not passed on the command line. Controls which `CLAUDE.md`, settings overrides, and MCP category selection are applied. |
| `AGENTIHOOKS_MCP_FILE` | -- | Absolute path to an MCP JSON file. When set, `agentihooks init` automatically merges the servers from this file into user-scope config (`~/.claude.json`). The path is recorded in `state.json` so subsequent runs re-apply it. Useful for CI/Docker automation where a gateway MCP file is injected at container start. |

---

## Global

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_HOME` | `~/.agentihooks` | Root directory for all runtime data: logs, memory, state. Set to a shared mount for Kubernetes deployments. |
| `MCP_CATEGORIES` | `all` | Comma-separated list of MCP tool categories to load. Valid values: `channels,enforcement` (see `hooks/mcp/_registry.py`). |
| `ALLOWED_TOOLS` | -- | Legacy: comma-separated list of specific tool names. Takes precedence over category filtering after categories are loaded. |
| `ENABLE_TOOL_SEARCH` | `true` | Set in the `env` block of `settings.json`. Makes all MCP tools lazy-loaded on demand -- shown as "(loaded on-demand)" in `/context`. Eliminates approximately 79K token upfront cost from MCP tool schemas. |

---

## MCP Transport

Only relevant when running `agentihooks` as a network server instead of a
per-session stdio subprocess -- for clients that filter stdio MCP servers out at
load time. See [MCP Transport](../hooks/mcp-transport.md) for the full setup.

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http`. An unrecognised value warns on stderr and falls back to `stdio`. |
| `MCP_HOST` | `localhost` | Bind address for network transports, and the host written into the `~/.claude.json` url. The loopback bind is the only access boundary -- neither transport carries authentication. Spelled `localhost` rather than `127.0.0.1` deliberately: an enterprise policy was observed dropping a dotted-quad entry from the client's configured-server set outright. See [MCP Transport]({{ site.baseurl }}/hooks/mcp-transport/). |
| `MCP_SCHEME` | `http` | Scheme in the `~/.claude.json` url. The daemon serves plaintext, which is correct for a loopback bind; set `https` only when a TLS reverse proxy fronts it. |
| `AGENTIHOOKS_MCP_SUPERVISOR` | `auto` | Daemon supervisor: `systemd` where a user bus exists, `pidfile` otherwise. Force either to exercise both paths on one machine; an unrecognised value warns on stderr and falls back to detection. Drive the daemon with `agentihooks mcp start\|stop\|restart\|status`. |
| `MCP_PORT` | `8642` | Bind port for network transports. |
| `MCP_SSE_PATH` | `/sse` | Event-stream path under `sse`. |
| `MCP_STREAMABLE_HTTP_PATH` | `/mcp` | Endpoint path under `streamable-http`. |
| `MCP_STATELESS_HTTP` | `false` | Stateless HTTP mode. Only relevant behind a non-sticky reverse proxy. |
| `MCP_SESSION_ID_BANNER_ENABLED` | `true` | Inject the session's own id at SessionStart, so the agent can pass it to the session-scoped tools (`channel_acknowledge`). Required under a network transport, where one process serves every session and the environment cannot identify the caller. |
| `AGENTIHOOKS_MCP_TRANSPORT` | -- | Read by `agentihooks init` only, overriding `MCP_TRANSPORT` for that run. Use for a one-off install without editing `~/.agentihooks/.env`. |

---

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_ENABLED` | `true` | Enable or disable hook logging entirely. |
| `CLAUDE_HOOK_LOG_FILE` | `~/.agentihooks/logs/hooks.log` | Hook event log file path. |
| `AGENT_LOG_FILE` | `~/.agentihooks/logs/agent.log` | Agent transcript log file path. |
| `AGENTIHOOKS_LOG_MAX_MB` | `20` | Size at which `hooks.log`, `agent.log` and `async-hooks.log` rotate. |
| `LIFECYCLE_GC_ENABLED` | `true` | Workspace lifecycle: per-call claims, disk warning, session-triggered sweeps and the hourly `agentihooks-gc` scheduler cron. `false` removes the cron on the next install. |
| `AGENTIHOOKS_GC_INTERVAL_MIN` | `60` | Minimum minutes between session-triggered sweeps. |
| `AGENTIHOOKS_DISK_WARN_GB` | `100` | Free space (smaller of `$HOME` and, on WSL, `/mnt/c`) below which every tool call is warned and a sweep starts. |
| `AGENTIHOOKS_LOG_BACKUPS` | `3` | Rotated copies kept per log (`.1` newest). `0` truncates instead. |
| `LOG_TRANSCRIPT` | `true` | Auto-log conversation transcript entries on each hook event. |
| `STREAM_AGENT_LOG` | `true` | Stream transcript to `AGENT_LOG_FILE` in real-time. |
| `LOG_HOOKS_COMMANDS` | `false` | Enable verbose command output logging. |
| `LOG_USE_COLORS` | `true` | ANSI colors in log output. Set `false` for CloudWatch Logs. |

---

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_AUTO_SAVE` | `true` | Auto-save session digest to memory store on `Stop` event. |
| `REDIS_URL` | -- | Redis connection string. Format: `redis://:PASSWORD@host:port/db`. Used by token monitor (burn rate), file read cache (dedup), and warning edge-triggers. All features degrade gracefully when Redis is unavailable. Uses DB0 on the shared Redis instance (same as agenticore). Leave unset to use in-memory/JSONL fallback. |
| `REDIS_SESSION_TTL` | `86400` | Session TTL in seconds (24 hours). |
| `REDIS_POSITION_TTL` | `3600` | Position TTL in seconds (1 hour). |
| `REDIS_KEY_PREFIX` | `agenticore` | Redis key prefix for all stored keys. |
| `REDIS_SOCKET_TIMEOUT` | `5.0` | Hook runtime Redis socket timeout in seconds. The hive endpoint does not read it. |

---

## Token Control

Controls the Token Control Layer, which reduces context window consumption in agentic sessions. All features are individually disableable. Requires Redis for burn-rate tracking and edge-trigger warnings; degrades gracefully without it.

| Variable | Default | Description |
|----------|---------|-------------|
| `TOKEN_CONTROL_ENABLED` | `true` | Master switch. Set `false` to disable all token control features at once. |
| `TOKEN_MONITOR_ENABLED` | `true` | Enable the `statusLine` script (`hooks/statusline.py`) context window monitor. Outputs a 3-line status bar (line 1: fill %, model, cost; line 2: token counts, burn rate, cache ratio, git branch; line 3: active agentihooks profile, settings-profile, channels subscription list from `AGENTIHOOKS_BASE_CHANNELS`) plus a conditional 4th line for threshold warnings or native rate limits. `used_pct` is recomputed from `total_input_tokens / context_window_size * 100` to avoid stale payload values. |
| `TOKEN_WARN_PCT` | `60` | Fill percentage at which a warning banner is injected into Claude's context. Edge-triggered: fires only once per session per threshold level. |
| `TOKEN_CRITICAL_PCT` | `80` | Fill percentage at which a critical banner is injected. |
| `TOKEN_REDIS_TTL` | `3600` | TTL (seconds) for Redis keys storing token metrics and warning state. |
| `BASH_FILTER_ENABLED` | `true` | Truncate verbose bash command output before it enters the context window. |
| `BASH_FILTER_MAX_LINES` | `50` | Line limit for docker/kubectl output (keeps last N lines). |
| `BASH_FILTER_MAX_CHARS` | `5000` | Character cap for install/build output. Other output (cat, git diff, grep) is never trimmed. |
| `BASH_FILTER_TEST_MAX_FAILURES` | `10` | Maximum FAILED blocks to retain from test runner output. |
| `BASH_FILTER_GIT_MAX_COMMITS` | `20` | Maximum commits to retain from `git log` output. |
| `FILE_READ_CACHE_ENABLED` | `true` | Block redundant re-reads of unmodified files within a session. Files modified since last read are always allowed through (mtime guard). |
| `FILE_READ_CACHE_BACKEND` | `redis` | Cache backend. `redis` uses the configured `REDIS_URL`; any other value forces the in-process memory fallback. |
| `FILE_READ_CACHE_TTL` | `21600` | TTL (seconds) for Redis cache keys (6 hours). |
| `MCP_HYGIENE_ENABLED` | `true` | Inject an MCP server usage reminder at `SessionStart` prompting Claude to disable unused servers via `/mcp`. |
| `CONTEXT_AUDIT_ENABLED` | `true` | Track cumulative byte output per tool type across the session. Emits an audit report on Stop when fill exceeds `CONTEXT_AUDIT_THRESHOLD_PCT`. |
| `CONTEXT_AUDIT_THRESHOLD_PCT` | `70` | Context fill % threshold for emitting the audit report on Stop. |
| `COMPACT_SUGGEST_ENABLED` | `true` | Replace generic "/compact" warnings with smart suggestions showing top context consumers from audit data. |
| `EFFORT_POLICY_ENABLED` | `true` | Inject thinking/effort guidance at `SessionStart` based on `DEFAULT_EFFORT`. Warns on PostToolUse when Agent tool spawns with unnecessarily expensive models. |
| `DEFAULT_EFFORT` | `medium` | Default reasoning effort level: `low`, `medium`, or `high`. Controls guidance injected at session start. |
| `THINKING_BUDGET_TOKENS` | `0` | Advisory thinking token ceiling per response. 0 = no limit. |
| `MCP_TOOL_WARN_THRESHOLD` | `40` | Warn at `SessionStart` if total MCP tools across all servers exceed this count. |
| `MCP_SCHEMA_AVG_TOKENS` | `150` | Estimated tokens per MCP tool schema (used in `agentihooks mcp report`). |

---

## CLAUDE.md Sanity Check

Guardrail that prevents agents from bloating `CLAUDE.md` and `CLAUDE.local.md` files past a configurable line limit. Runs on every `PreToolUse` event for `Write` and `Edit` tools. **Enabled by default.**

For `Write`: counts lines in the new content. For `Edit`: reads the current file from disk, simulates the replacement, and counts resulting lines. If the result exceeds the limit, the tool call is blocked (exit code `2`) with a message telling the agent the current/resulting line count and the cap.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK` | `true` | Enable/disable the guardrail. Set `false` or `0` to allow unrestricted edits. |
| `AGENTIHOOKS_CLAUDE_MD_MAXLINES` | `200` | Maximum allowed lines in `CLAUDE.md` / `CLAUDE.local.md` files. |

An operator can raise the cap for the current session by including
`claude-md-max-lines=<number>` anywhere in a message. The value cannot lower the
configured baseline, and the override clears when the session ends.

---

## Context Preprocessor (token compression)

> **Periodic rules / CLAUDE.md re-injection was removed on 2026-07-20.** Claude
> Code loads `~/.claude/CLAUDE.md`, the project `CLAUDE.md`, and every
> `~/.claude/rules/*.md` at position 0 for the whole session. Re-emitting those
> same bytes every N turns spent thousands of duplicate tokens per session and
> broke the prompt-cache prefix on each refresh, for a re-emphasis the brain
> drumbeat and the [enforcement](../pillars/guardrails.md) cadence already
> provide more cheaply. Live re-emphasis of *edited* rules still ships to running
> sessions via the one-shot `agentihooks refresh-rules` path.

The token-compression preprocessor survives — it still compresses injected
broadcast banners and verbose tool output. The variables keep their historical
`CONTEXT_REFRESH_*` names for config back-compat; they no longer gate any
periodic re-injection.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_REFRESH_COMPRESSION` | `standard` | Compression level: `off`, `light`, `standard`, `aggressive`. See [Context Preprocessor](../hooks/context-preprocessor.md). |
| `CONTEXT_COMPRESSION_SCOPE` | `refresh` | `all` compresses all injections + tool output. `refresh` (default) is a **no-op** — it once scoped compression to the removed context-refresh payload; set `all` to get any compression. |
| `CONTEXT_REFRESH_ABBREV_FILE` | *(empty)* | Path to user-supplied abbreviation dictionary (JSON). Merged on top of built-in. |

---

## Broadcast System

| Variable | Default | Description |
|----------|---------|-------------|
| `BROADCAST_ENABLED` | `true` | Enable/disable the broadcast system. |
| `BROADCAST_FILE` | `~/.agentihooks/broadcast.json` | Path to the broadcast file. Set to a shared mount for multi-node deployments. |
| `BROADCAST_MAX_MESSAGES` | `50` | Maximum concurrent broadcasts. Oldest expire first when limit is reached. |
| `BROADCAST_CRITICAL_ON_PRETOOL` | `true` | Inject critical broadcasts on `PreToolUse` via `additionalContext` JSON. |
| `AGENTIHOOKS_BASE_CHANNELS` | `""` (empty) | Comma-separated channel subscription floor. Default ships in `profiles/default/.claude/settings.overrides.json` env block as `"brain,amygdala"`. Layered: profile env → repo `.claude/settings.json` env → repo `.claude/settings.local.json` env → container launch ENV (highest). Empty / unset → session only receives global broadcasts (no `channel` field). Wildcard `*` subscribes to everything. Whitespace and duplicates are trimmed/deduped at parse time. Mid-session change requires a session restart. |

See [Broadcast System](../hooks/broadcast.md) for full architecture and CLI documentation.

---

## Enforcement System

| Variable | Default | Description |
|----------|---------|-------------|
| `ENFORCEMENT_INJECTION_ENABLED` | `true` | Enable SessionStart, first-delivery, and cadence-driven enforcement injection. |
| `ENFORCEMENT_FILE` | `~/.agentihooks/enforcements.json` | Mutable global runtime enforcement store. |
| `ENFORCEMENT_COUNTER_FILE` | `~/.agentihooks/enforcement_counters.json` | Per-session tool-call counters used for cadence. |
| `ENFORCEMENT_DELIVERY_STATE_FILE` | `~/.agentihooks/enforcement_delivery_state.json` | Per-session IDs already seen, shared across prompt and tool hooks. |
| `ENFORCEMENT_MATCH_COUNTER_FILE` | `~/.agentihooks/enforcement_match_counters.json` | Per-session, per-entry counts of matching tool calls for enforcements with a `matcher`. |

---

## Conditions

| Variable | Default | Description |
|----------|---------|-------------|
| `CONDITIONS_ENABLED` | `true` | Run bundle and profile [conditions](../hooks/conditions.md) on PreToolUse and PostToolUse. |
| `CONDITIONS_TIMEOUT_SEC` | `10` | Per-condition timeout; the condition's process group is killed on expiry and the call proceeds. |
| `CONDITIONS_MAX_PARALLEL` | `8` | Synchronous conditions run at once for one tool call. |
| `CONDITIONS_TRUSTED_OWNERS` | `""` | Extra git remote owners whose repositories' `.agentihooks/conditions/` may run (`*` = all). Repos without a remote and repos owned like the linked bundle are always trusted. |

---

## Account Load Balancing

See [Claude Account Load Balancing](../pillars/load-balancing.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `AH_CC_TOKEN_<slug>` | — | One Claude subscription OAuth token per account; `agentihooks claude login <slug>` writes it from `claude setup-token` |
| `NO_COLOR` | — | Set to any value, even empty, turns off the coloured output of `agentihooks claude login`, `codex login` and `codex refresh` |
| `AH_CX_TOKEN_<slug>` | — | One Codex ChatGPT workspace access token per account; once any is set the default `codex login` account leaves the pool. `agentihooks codex login <slug>` writes it |
| `AGENTIHOOKS_AICODE_TOKEN_REFRESH` | `false` | `true` makes this machine the refresh authority: `agentihooks codex refresh` and the session-start kick run only here. `agentihooks codex login` sets it |
| `AGENTIHOOKS_AICODE_TOKEN_STORE` | `local` | Where the refresher reads and writes accounts: `local` (`~/.agentihooks/codex/<slug>/auth.json`, then the rc block, then OpenBao when configured) or `openbao` (the OpenBao secret only) |
| `AGENTIHOOKS_AICODE_TOKEN_REFRESH_DAYS` | `3` | Refresh an account whose access token has fewer days left |
| `AGENTIHOOKS_CODEX_READING_MAX_AGE_H` | `6` | Hours a Codex quota reading keeps setting the account's cap while its 7-day window has not reset |
| `AGENTIHOOKS_AICODE_TOKEN_REFRESH_INTERVAL_H` | `6` | Hours between the refreshes a session start kicks off |
| `OPENBAO_URL`, `OPENBAO_TOKEN` | — | OpenBao address and token for the KV v2 secrets at `AGENTIHOOKS_CODEX_CREDS_PATH` (written by `codex login` and the refresher) and `secret/agentihooks/claude-creds` (written by `claude login`), both read by `accounts env` |
| `AGENTIHOOKS_CODEX_CREDS_PATH` | `agentihooks/codex-creds` | KV v2 path on the `secret` mount holding every `AH_CX_TOKEN_<slug>` and `AH_CX_REFRESH_<slug>`, read by `accounts env` and written by `codex login` and `codex refresh` |
| `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` | — | Offer a Claude `api` slot: a `CLAUDE_CODE_USE_*` platform flag, a foreign `ANTHROPIC_BASE_URL` with either credential, or `ANTHROPIC_API_KEY` alone. Token children never inherit them |
| `CODEX_API_KEY`, `OPENAI_API_KEY` | — | Either offers a Codex `api` slot (first set wins) |
| `AH_CX_API_BASE_URL`, `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the Codex api provider; must carry no user, password, query or fragment |
| `AH_ROUTE_API` | set by the router | Name-only marker on an api child; the process counts on account `api` and the quota policy skips it |
| `AGENTIHOOKS_ROUTE_ACCOUNT` | set by the router | Account a Claude launch was routed to (`api` for the api side) |
| — | — | Api weights and caps are routing settings, not variables: `agentihooks balance set` (see [CLI Commands](cli-commands.md#agentihooks-balance)) |
| — | — | Each account's live session cap comes from its five-hour window alone: 6 at 60%+ left, 4 at 40-60%, 3 at 10-40%, 2 at 5-10%, none below 5% until reset; no new session below 5% of the week. The next session goes to the eligible account with the fewest live sessions, ties to the soonest resetting week. |
| `AGENTIHOOKS_HIVE_SESSION_CEILING` | `12` | Most live sessions a hive reports for one Claude subscription; the hive cap is this ceiling times the smaller of five hour and week left, at least the band unless the ceiling is lower |
| `QUOTA_POLICY_ENABLED` | `true` | Hand off / wait / stop when a session's own quota runs out |
| `AGENTIHOOKS_HANDOFF_WEEK_PCT` | `98` | 7-day used % that triggers the quota policy |
| `AGENTIHOOKS_HANDOFF_5H_PCT` | `99` | 5-hour used % that triggers the quota policy |
| `AGENTIHOOKS_HANDOFF_MIN_LEFT` | `20` | Routing left % (tighter of 5h and 7d) a handoff target needs |
| `AGENTIHOOKS_WAIT_MIN_WEEK_LEFT` | `10` | 7-day left % that makes a spent 5-hour window wait for its reset instead of stopping |
| `QUOTA_USAGE_INJECTION_ENABLED` | `true` | Inject the [quota usage banner](../pillars/load-balancing.md#quota-usage-banner) on every prompt and on the tool call cadence |
| `QUOTA_USAGE_TOOL_CALLS` | `5` | Tool calls between quota usage banners |
| `QUOTA_USAGE_STALE_SEC` | `300` | Age in seconds after which a quota reading is ignored by the banner and the quota policy |

---

## Hook Injection Budget

Claude hook context uses a shared event budget, independently of compression.
See [Hook Injection Budget](../hooks/injection-budget.md) for priority order,
examples, inbox exceptions and size diagnostics. Codex and Copilot retain their
own limits and arrival order.

| Variable | Default | Description |
|----------|---------|-------------|
| `HOOK_INJECTION_MAX_CHARS` | `9000` | Claude event budget in UTF-16 code units. Keep positive and below 10,000; zero or negative disables fitting and priority sorting. |

---

## Tool Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_TOOL_MEMORY_PATH` | `~/.agenticore_tool_memory.ndjson` | Path to the tool error memory file. |
| `AGENTICORE_TOOL_MEMORY_MAX` | `100` | Maximum number of error entries to store. |
| `AGENTICORE_TOOL_MEMORY_SHOW` | `5` | Maximum selected entries per `PreToolUse` event, each limited to 160 characters before event budget fitting. |

---

## Session Context

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_CORRELATION_ID` | -- | Correlation ID for distributed tracing. Injected into outgoing payloads. |
| `AGENTICORE_CLAUDE_SESSION_ID` | -- | Claude Code session ID override. |
| `AGENTICORE_AGENT` | `unknown` | Agent identifier tag. |
| `AGENT_NAME` | `Agent` | Agent display name for notifications and logs. |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | -- | Output token limit. Injected into session context if set. |

---

## GitHub CLI Config Pin

gh 2.x moves the operator's login: the gh 2.4.0 binary on the operator's
machine carries `autoMigrateConfigDir`, recorded in #3502. When
`XDG_CONFIG_HOME` is set, `GH_CONFIG_DIR` is unset and `$XDG_CONFIG_HOME/gh`
does not exist, gh renames `~/.config/gh` into `$XDG_CONFIG_HOME/gh` on its
next run. A proof, render or
launch that points `XDG_CONFIG_HOME` at a scratch folder then takes the login
with it, and every other session on the machine loses GitHub until the folder is
moved back. This happened twice on 15 October: once from a proof wrapper and
once from the Claude session it had already started
([#3502](https://github.com/the-cloud-clockwork/agentihooks/issues/3502), fixed by
[#3505](https://github.com/the-cloud-clockwork/agentihooks/pull/3505)).

AgentiHooks pins `GH_CONFIG_DIR` to the home gh folder whenever
`XDG_CONFIG_HOME` points away from the home default, so gh reads the operator's
login in place and never migrates it.

| Variable | Default | Description |
|----------|---------|-------------|
| `GH_CONFIG_DIR` | -- | Read by gh. AgentiHooks sets it to `$HOME/.config/gh` when `XDG_CONFIG_HOME` is set to anything other than `$HOME/.config` and `GH_CONFIG_DIR` is unset or empty. A value you set is kept. |
| `XDG_CONFIG_HOME` | -- | Decides whether the pin applies. Unset, or equal to `$HOME/.config`, leaves the environment unchanged. |
| `CLAUDE_ENV_FILE` | -- | Set by Claude Code for each session. When a hook pins `GH_CONFIG_DIR`, it appends `export GH_CONFIG_DIR=...` to this file once, so the agent's own shell commands inherit the pin. |

### Where the pin applies

| Process | How |
|---------|-----|
| Every `agentihooks` CLI command and every `scripts` module | Importing the `scripts` package pins its own environment and every child it starts. |
| Every hook | The hook process pins its environment after loading `~/.agentihooks/.env` and `~/.agentihooks/*.env`, which may set `XDG_CONFIG_HOME`. |
| A Claude session's shell | The hook writes the pin to `CLAUDE_ENV_FILE`, which covers a session that was not started by an AgentiHooks launcher. The line is written once; an existing copy is left alone. |
| Agents opened by `init-agent`, including swarm spawns and handoffs | The launcher script runs the same check in shell before it starts Claude or Codex. |

The pin never stops a hook. If `CLAUDE_ENV_FILE` cannot be read, decoded or
written, the hook continues without it.

### Isolated proofs you write

A wrapper that sets `XDG_CONFIG_HOME` and runs commands outside AgentiHooks must
pin gh itself, in every shell and session it starts. The engineer role carries
this rule.

```bash
export XDG_CONFIG_HOME="$SCRATCH/config"
export XDG_STATE_HOME="$SCRATCH/state"
export XDG_DATA_HOME="$SCRATCH/data"
export GH_CONFIG_DIR="$HOME/.config/gh"
```

The same check the launcher writes, for a script that may or may not be isolated:

```bash
if [ -n "${XDG_CONFIG_HOME:-}" ] && [ "$XDG_CONFIG_HOME" != "$HOME/.config" ]; then
  export GH_CONFIG_DIR="${GH_CONFIG_DIR:-$HOME/.config/gh}"
fi
```

### Limits

- A session started before the pin shipped keeps its old environment. A long
  lived isolated session runs gh too, for example for pull request status, so it
  needs the pin as much as a new command does. Restart it.
- The pin only fills an unset value. A `GH_CONFIG_DIR` pointing elsewhere is
  used as given.
- An `XDG_CONFIG_HOME` of `$HOME/.config/`, with a trailing slash, is pinned by
  the launcher's shell check, which compares text, and left alone by hooks and
  the CLI, which compare paths. Both outcomes keep the login in place.

### Troubleshooting

Every agent reports gh as logged out at once:

1. Check whether the home folder is gone: `test -d ~/.config/gh && echo present || echo missing`.
2. Look for a moved copy anywhere under your home folder:
   `find ~ -maxdepth 8 -type d -name gh -not -path "$HOME/.config/gh"`.
   Isolated proofs usually put their config folder under `~/scratchpad`. The moved `gh`
   folder keeps its original modification time; its new parent `config` folder
   carries the time of the move.
3. Confirm nothing has recreated the home folder, `test ! -e ~/.config/gh && echo free`,
   since `mv` would otherwise nest the login inside it. Move it back with
   `mv <found folder> ~/.config/gh`, then confirm with
   `gh api user --jq .login`. List the folder rather than reading its files: it
   holds the login token.
4. The scratch folder that held the moved copy names the proof or session that
   set `XDG_CONFIG_HOME`. Add the pin to that wrapper and restart any session it
   started, which still carries the unpinned environment.

---

## Email / SMTP

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_SERVER` | -- | SMTP server hostname. |
| `SMTP_PORT` | `25` | SMTP port. |
| `SMTP_SERVER_IP` | -- | Optional fallback IP for the SMTP server. |
| `SMTP_USER` | -- | SMTP username (authenticated mode only). |
| `SMTP_PASS` | -- | SMTP password (authenticated mode only). |
| `SENDER_EMAIL` | -- | From address for all outgoing email. |

---

## Storage (S3)

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_URL` | -- | S3 URL or endpoint (e.g., `s3://my-bucket`). |
| `IS_EVALUATION` | `false` | Evaluation mode -- skips actual upload. |

---

## Database

### DynamoDB

| Variable | Default | Description |
|----------|---------|-------------|
| `DYNAMODB_TABLE_NAME` | -- | DynamoDB table name. |
| `DYNAMODB_PARTITION_KEY` | `session_id` | Partition key attribute name. |
| `DYNAMODB_SORT_KEY` | -- | Sort key attribute name. Omit for tables with no sort key. |
| `DYNAMODB_ENDPOINT_URL` | -- | Custom DynamoDB endpoint (for DynamoDB Local in testing). |
| `IS_EVALUATION` | `false` | Evaluation mode -- skips actual write. |

### PostgreSQL

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_HOST` | -- | Database host. |
| `POSTGRES_NAME` | -- | Database name. |
| `POSTGRES_USERNAME` | -- | Username. |
| `POSTGRES_PASSWORD` | -- | Password. |
| `POSTGRES_PORT` | `5432` | Port. |
| `POSTGRES_TABLE` | -- | Default table name. |
| `IS_EVALUATION` | `false` | Evaluation mode -- skips actual write. |

---

## Compute (Lambda)

| Variable | Default | Description |
|----------|---------|-------------|
| `LAMBDA_FUNCTION_NAME` | -- | Lambda function ARN or name. |
| `LAMBDA_INVOCATION_TYPE` | `RequestResponse` | Default invocation type. `RequestResponse` (sync) or `Event` (async). |
| `IS_EVALUATION` | `false` | Evaluation mode -- skips actual invocation. |

---

## AWS (Config reader)

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_CONFIG_FILE` | `~/.aws/config` | Path to AWS config file. |

---

## Evaluation mode

`IS_EVALUATION=true` is a shared flag recognized by storage, database, and compute integrations. When set, tools skip their actual external calls and return a simulated success response. Useful for testing agent logic without side effects.

| Variable | Integrations affected |
|----------|-----------------------|
| `IS_EVALUATION` | S3, DynamoDB, PostgreSQL, Lambda |

---

## Master Inbox Intent Grouping

See [Intent grouping](../pillars/swarm.md#intent-grouping). Read by every process that hands out a swarm's master inbox pool.

| Variable | Default | Description |
|----------|---------|-------------|
| `MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS` | `on` | `1`, `true`, `yes` or `on` keep grouping on; any other value turns it off and the pool deals items round robin |
| `MASTER_INBOX_CLASSIFIER_TIMEOUT_S` | `3` | Seconds one pool pass waits for its intent verdicts before unanswered items go round robin |

---

## Swarm Grafana Alerts

See [Grafana bottleneck alerts](grafana-bottleneck-alerts.md). The swarm tick reads alert state through the gateway with these; no listener, token or secret path of its own.

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_KEY_GATEWAY` | — | LiteLLM gateway key; its LiteLLM unit must reach the `grafana` MCP server. Unset → the tick reads no alerts |
| `LANGFUSE_MCP_URL` | — | Gateway MCP endpoint, shared with the Langfuse summary pass. Unset → the tick reads no alerts |

---

## Brain Adapter

Every variable in this section is owned by Agentibrain and stored in
~/.agentibrain/.env. AgentiHooks consumes the allowlisted values without
duplicating them into its own environment file.

| Variable | Default | Description |
|----------|---------|-------------|
| `BRAIN_ENABLED` | `true` when `BRAIN_URL` is set, else `false` | Enable the brain adapter for knowledge injection via broadcast channels. The legacy default also flips to `true` when `~/.agentihooks/brain-feed/` holds `.md` files. An explicit value always wins. |
| `AGENTIBRAIN_HOME` | `~/.agentibrain` | The brain's own config directory. Its `.env` is the source for every `BRAIN_*`, `AMYGDALA_*`, and brain credential setting consumed by AgentiHooks. Database and provider credentials are not adopted into the hook process. |
| `BRAIN_URL` | discovered from `$AGENTIBRAIN_HOME/.env` | Base URL of brain-api. Setting it moves the adapter off the filesystem onto HTTP and turns the reader and writer on by default. |
| `BRAIN_HTTP_TOKEN` | falls back to `KB_ROUTER_TOKEN` | Bearer token for brain-api. Without it every read and every marker POST answers 401. |
| `BRAIN_WRITER_ENABLED` | `true` when `BRAIN_URL` is set, else `false` | POST `@lesson` / `@signal` / `@decision` / `@milestone` markers to `{BRAIN_URL}/marker` on session Stop. |
| `BRAIN_WRITER_OUTBOX` | `~/.agentihooks/brain-outbox` | Local buffer for markers that could not be POSTed. Drains on the next session Stop after brain-api is reachable. |
| `BRAIN_SOURCE_TYPE` | `file` | Legacy fallback backend used only when `BRAIN_URL` is unset. |
| `BRAIN_SOURCE_PATH` | `~/.agentihooks/brain-feed` | Directory containing fallback brain `.md` files (YAML frontmatter + markdown body). |
| `BRAIN_CHANNEL` | `brain` | Broadcast channel the brain adapter publishes to (producer side). Sessions only receive brain content if this channel name is in their `AGENTIHOOKS_BASE_CHANNELS` env. The default profile ships `"brain,amygdala"`, so the default `brain` value works out of the box; if you rename this, update subscriptions to match. |
| `BRAIN_HOT_ARCS_TOP_N` | `10` | Maximum hot arcs included in the injected hot-arcs table. |
| `BRAIN_PAYLOAD_MAX_BYTES` | `1536` | Maximum characters injected from each brain feed entry. |
| `BRAIN_REFRESH_TOOL_CALLS` | `20` | Reconcile brain content every N tool calls (counter-gated). |

---


## Guardrails

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_SECRETS_MODE` | `standard` | Secrets scanning mode: `off`, `warn`, `standard`, `strict`. |
| `RETRY_BREAKER_ENABLED` | `true` | Enable the retry circuit breaker guardrail. |
| `RETRY_BREAKER_MAX` | `5` | Soft limit -- warn after this many identical failures. |
| `RETRY_BREAKER_HARD_MAX` | `10` | Hard limit -- block after this many identical failures. |
| `RETRY_BREAKER_TTL` | `300` | Seconds before a failure fingerprint expires from the counter. |
| `CONTROLS_BYPASS_ENABLED` | `true` | Enable the controls toggle (bypass mode). Operator phrase `disable controls` lifts branch, non-protected PR, hotfix and non-main force-push gates session-wide. Protected promotion remains CI-only; PyPI publication still requires `release dance`. See [Guardrail 9](/docs/pillars/guardrails/#guardrail-9-controls-toggle-bypass-mode). |
| `KUBECTL_MUTATION_GUARD_ENABLED` | `true` | HARD-FLOOR live-patching guard. PreToolUse Bash hook that blocks `kubectl exec`/`cp` writes, `kubectl edit`/`patch`/`set`/`scale`/`rollout-restart`/`annotate`/`label`/`drain`/`debug`/`create -f`/`apply -f`/`autoscale`, `helm install`/`upgrade`/`rollback` outside CI, `argocd app sync --local`, SSH-edit, `scp` INTO host, and `docker exec` writes. Does NOT honor bypass mode, hotfix signals, or release-gate signals — code is the source of truth. See CI Manifesto §3.5 and `rules/code-is-source.md`. |
| `AGENTIHOOKS_SERENA` | `true` | Serena edit guard. PreToolUse hook that blocks a built-in or shell rewrite of Python in a linked worktree, routing it to Serena's symbol tools instead. `false` turns the whole guard off for that session or machine: every edit is let through and no Serena directive is injected; unset or any other value keeps it on. See Development Manifesto §7 and `rules/code-intelligence.md`. |
