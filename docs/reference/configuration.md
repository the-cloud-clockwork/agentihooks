---
title: Configuration
nav_order: 2
---

# Configuration Reference
{: .no_toc }

All environment variables recognized by AgentiHooks, grouped by integration. Variables with no default are required for their integration to function.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Install & Automation

These variables control how `agentihooks init` installs and configures Claude Code. They are read at install time, not at hook runtime.

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
| `MCP_CATEGORIES` | `all` | Comma-separated list of MCP tool categories to load. Valid values: `aws,email,storage,database,compute,observability,utilities`. |
| `ALLOWED_TOOLS` | -- | Legacy: comma-separated list of specific tool names. Takes precedence over category filtering after categories are loaded. |
| `ENABLE_TOOL_SEARCH` | `true` | Set in the `env` block of `settings.json`. Makes all MCP tools lazy-loaded on demand -- shown as "(loaded on-demand)" in `/context`. Eliminates approximately 79K token upfront cost from MCP tool schemas. |

---

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_ENABLED` | `true` | Enable or disable hook logging entirely. |
| `CLAUDE_HOOK_LOG_FILE` | `~/.agentihooks/logs/hooks.log` | Hook event log file path. |
| `AGENT_LOG_FILE` | `~/.agentihooks/logs/agent.log` | Agent transcript log file path. |
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
| `REDIS_SOCKET_TIMEOUT` | `5.0` | Redis socket timeout in seconds. |

---

## Token Control

Controls the Token Control Layer, which reduces context window consumption in agentic sessions. All features are individually disableable. Requires Redis for burn-rate tracking and edge-trigger warnings; degrades gracefully without it.

| Variable | Default | Description |
|----------|---------|-------------|
| `TOKEN_CONTROL_ENABLED` | `true` | Master switch. Set `false` to disable all token control features at once. |
| `TOKEN_MONITOR_ENABLED` | `true` | Enable the `statusLine` script (`hooks/statusline.py`) context window monitor. Outputs a 2-line status bar (fill %, burn rate, cost, cache ratio, git branch) plus a conditional threshold warning line. `used_pct` is recomputed from `total_input_tokens / context_window_size * 100` to avoid stale payload values. |
| `TOKEN_WARN_PCT` | `60` | Fill percentage at which a warning banner is injected into Claude's context. Edge-triggered: fires only once per session per threshold level. |
| `TOKEN_CRITICAL_PCT` | `80` | Fill percentage at which a critical banner is injected. |
| `TOKEN_REDIS_TTL` | `3600` | TTL (seconds) for Redis keys storing token metrics and warning state. |
| `BASH_FILTER_ENABLED` | `true` | Truncate verbose bash command output before it enters the context window. |
| `BASH_FILTER_MAX_LINES` | `50` | Line limit for docker/kubectl output (keeps last N lines). |
| `BASH_FILTER_MAX_CHARS` | `5000` | Character cap for build and generic output. |
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
| `PEAK_HOURS_ENABLED` | `true` | Show peak/off-peak indicator on statusline line 3. Detects Anthropic peak billing hours (weekday business hours). |
| `PEAK_HOURS_START` | `9` | Peak start hour (in target timezone). |
| `PEAK_HOURS_END` | `17` | Peak end hour (exclusive). |
| `PEAK_HOURS_TZ` | `US/Pacific` | IANA timezone name for peak hour detection. |
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

---

## Context Refresh

Combats attention decay in long sessions by periodically re-injecting rules and CLAUDE.md into the LLM's context window. Rules and CLAUDE.md are injected on separate cadences so they don't bloat a single turn.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_REFRESH_ENABLED` | `true` | Enable/disable periodic re-injection on `UserPromptSubmit`. |
| `CONTEXT_REFRESH_INTERVAL` | `20` | Re-inject rules every N user messages. |
| `CONTEXT_REFRESH_CLAUDE_MD_INTERVAL` | `40` | Re-inject CLAUDE.md every N user messages. Set `0` to disable CLAUDE.md refresh. |
| `CONTEXT_REFRESH_RULES_DIR` | `~/.claude/rules` | Global rules directory (all profile layers merged here at install time). |
| `CONTEXT_REFRESH_INCLUDE_PROJECT` | `true` | Also inject project-level `.claude/rules/*.md` from the active working directory. |
| `CONTEXT_REFRESH_MAX_CHARS` | `8000` | Max characters per injection (~2000 tokens). Excess rules are truncated by priority. |
| `CONTEXT_REFRESH_COMPRESSION` | `standard` | Compression level: `off`, `light`, `standard`, `aggressive`. See [Context Preprocessor](../hooks/context-preprocessor.md). |
| `CONTEXT_COMPRESSION_SCOPE` | `refresh` | Where compression applies: `refresh` (context refresh only) or `all` (all injections + tool output). |
| `CONTEXT_REFRESH_ABBREV_FILE` | *(empty)* | Path to user-supplied abbreviation dictionary (JSON). Merged on top of built-in. |

### How it works

1. A turn counter increments on every `UserPromptSubmit` hook event (persisted via Redis or file fallback).
2. When `turn % CONTEXT_REFRESH_INTERVAL == 0`, all `*.md` files from the global rules dir (and optionally the project rules dir) are sorted by frontmatter `priority: N` (lower = higher priority, default 5), compressed via the [Context Preprocessor](../hooks/context-preprocessor.md) (default level: `standard`), and injected as a `system-reminder` banner. Project rules dir is resolved from the hook payload's `cwd` field (the session's active directory).
3. When `turn % CONTEXT_REFRESH_CLAUDE_MD_INTERVAL == 0`, `~/.claude/CLAUDE.md` (and optionally the project's `CLAUDE.md`) are compressed and injected as a separate banner.
4. Each injection is capped at `CONTEXT_REFRESH_MAX_CHARS`. Rules that exceed the cap are omitted with a count — highest-priority rules are always included first.

### Best practices for rule files

- **Keep rules concise.** Each rule file should be under 800 characters. If a rule needs more, split it into focused files.
- **Use `priority:` frontmatter for ordering.** Add `priority: N` to YAML frontmatter (lower = higher priority). Critical rules like delegation and security should be `priority: 1`. This ensures they survive truncation when the budget is tight. Default priority is 5.
- **One concern per file.** Don't merge delegation, security, and domain rules into a single large file. Smaller files give the truncation logic finer granularity.
- **Strip redundancy.** If CLAUDE.md and a rule file say the same thing, remove it from one. The refresh system injects both — duplication wastes budget.
- **Monitor the cap.** If you see `[N rule(s) omitted]` in your logs, either raise `CONTEXT_REFRESH_MAX_CHARS` or trim your rules.

---

## Broadcast System

| Variable | Default | Description |
|----------|---------|-------------|
| `BROADCAST_ENABLED` | `true` | Enable/disable the broadcast system. |
| `BROADCAST_FILE` | `~/.agentihooks/broadcast.json` | Path to the broadcast file. Set to a shared mount for multi-node deployments. |
| `BROADCAST_MAX_MESSAGES` | `50` | Maximum concurrent broadcasts. Oldest expire first when limit is reached. |
| `BROADCAST_CRITICAL_ON_PRETOOL` | `true` | Inject critical broadcasts on `PreToolUse` via `additionalContext` JSON. |

See [Broadcast System](../hooks/broadcast.md) for full architecture and CLI documentation.

---

## Sync Daemon

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_SYNC_POLL_SEC` | `60` | How often (seconds) the sync daemon polls for source file changes. |

---

## Tool Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_TOOL_MEMORY_PATH` | `~/.agenticore_tool_memory.ndjson` | Path to the tool error memory file. |
| `AGENTICORE_TOOL_MEMORY_MAX` | `100` | Maximum number of error entries to store. |
| `AGENTICORE_TOOL_MEMORY_SHOW` | `15` | Number of entries to inject per `PreToolUse` event. |

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

## Brain Adapter

| Variable | Default | Description |
|----------|---------|-------------|
| `BRAIN_ENABLED` | `false` | Enable the brain adapter for knowledge injection via broadcast channels. |
| `BRAIN_SOURCE_TYPE` | `file` | Brain source backend. Currently only `file` is shipped. |
| `BRAIN_SOURCE_PATH` | `~/.agentihooks/brain` | Directory containing brain `.md` files (YAML frontmatter + markdown body). |
| `BRAIN_CHANNEL` | `brain` | Broadcast channel for brain content delivery. |
| `BRAIN_REFRESH_INTERVAL` | `30` | Re-inject brain content every N turns (counter-gated). |

---

## Runtime Overlays

| Variable | Default | Description |
|----------|---------|-------------|
| `OVERLAY_INJECTION_ENABLED` | `true` | Enable mid-session profile overlay injection via UserPromptSubmit hook. |

---

## Guardrails

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_SECRETS_MODE` | `standard` | Secrets scanning mode: `off`, `warn`, `standard`, `strict`. |
| `RETRY_BREAKER_ENABLED` | `true` | Enable the retry circuit breaker guardrail. |
| `RETRY_BREAKER_MAX` | `5` | Soft limit -- warn after this many identical failures. |
| `RETRY_BREAKER_HARD_MAX` | `10` | Hard limit -- block after this many identical failures. |
| `RETRY_BREAKER_TTL` | `300` | Seconds before a failure fingerprint expires from the counter. |
| `CONTROLS_BYPASS_ENABLED` | `true` | Enable the controls toggle (bypass mode). Operator phrase `disable controls` short-circuits branch / PR / release-merge / hotfix / non-main force-push gates session-wide. See [Guardrail 9](/docs/pillars/guardrails/#guardrail-9-controls-toggle-bypass-mode). |
| `KUBECTL_MUTATION_GUARD_ENABLED` | `true` | HARD-FLOOR live-patching guard. PreToolUse Bash hook that blocks `kubectl exec`/`cp` writes, `kubectl edit`/`patch`/`set`/`scale`/`rollout-restart`/`annotate`/`label`/`drain`/`debug`/`create -f`/`apply -f`/`autoscale`, `helm install`/`upgrade`/`rollback` outside CI, `argocd app sync --local`, SSH-edit, `scp` INTO host, and `docker exec` writes. Does NOT honor bypass mode, hotfix signals, or release-gate signals — code is the source of truth. See CI Manifesto §3.5 and `rules/code-is-source.md`. |
