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

These variables control how `agentihooks global` and `agentihooks project` install and configure Claude Code. They are read at install time, not at hook runtime.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_CODE_HOME_DIR` | `$HOME` | Home-directory root override. When set, agentihooks targets `$CLAUDE_CODE_HOME_DIR/.claude` for all install operations. Use when `$HOME` differs from where Claude Code stores its config (e.g. shared volumes). |
| `AGENTIHOOKS_CLAUDE_HOME` | `~/.claude` | Legacy: direct path to the `.claude` directory. `CLAUDE_CODE_HOME_DIR` takes priority if both are set. |
| `AGENTIHOOKS_PROFILE` | `default` | Profile to use when `--profile` is not passed on the command line. Controls which `CLAUDE.md`, settings overrides, and MCP category selection are applied. |
| `AGENTIHOOKS_MCP_FILE` | — | Absolute path to an MCP JSON file. When set, `agentihooks global` automatically merges the servers from this file into user-scope config (`~/.claude.json`). The path is recorded in `state.json` so subsequent `agentihooks global` or `agentihooks --sync` re-applies it. Useful for CI/Docker automation where a gateway MCP file is injected at container start. |

---

## Global

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_HOME` | `~/.agentihooks` | Root directory for all runtime data: logs, memory, state. Set to a shared mount for Kubernetes deployments. |
| `MCP_CATEGORIES` | `all` | Comma-separated list of MCP tool categories to load. Valid values: `github,confluence,aws,email,messaging,storage,database,compute,observability,smith,agent,utilities`. |
| `ALLOWED_TOOLS` | — | Legacy: comma-separated list of specific tool names. Takes precedence over category filtering after categories are loaded. |

---

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_ENABLED` | `true` | Enable or disable hook logging entirely. |
| `CLAUDE_HOOK_LOG_FILE` | `~/.agentihooks/logs/hooks.log` | Hook event log file path. |
| `AGENT_LOG_FILE` | `~/.agentihooks/logs/agent.log` | Agent transcript log file path. |
| `LOG_TRANSCRIPT` | `true` | Auto-log conversation transcript entries on each hook event. |
| `STREAM_AGENT_LOG` | `true` | Stream transcript to `AGENT_LOG_FILE` in real-time. |
| `LOG_HOOKS_COMMANDS` | `false` | Enable `log_command_output` writes (verbose mode). |
| `LOG_USE_COLORS` | `true` | ANSI colors in log output. Set `false` for CloudWatch Logs. |

---

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_AUTO_SAVE` | `true` | Auto-save session digest to memory store on `Stop` event. |
| `REDIS_URL` | — | Redis connection string for session state and memory. Leave unset to use JSONL file storage only. |
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
| `TOKEN_MONITOR_ENABLED` | `true` | Enable the `StatusLine` context window monitor. Outputs `ctx: X/1M (Y%) \| burn: ZK/turn \| model: ...` to the terminal status bar. |
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
| `AGENTICORE_CORRELATION_ID` | — | Correlation ID for distributed tracing. Injected into outgoing payloads. |
| `AGENTICORE_CLAUDE_SESSION_ID` | — | Claude Code session ID override. |
| `AGENTICORE_AGENT` | `unknown` | Agent identifier tag. |
| `AGENT_NAME` | `Agent` | Agent display name for notifications and logs. |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | — | Output token limit. Injected into session context if set. |

---

## GitHub

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | — | Personal access token. Used when GitHub App credentials are not set. |
| `GITHUB_APP_ID` | — | GitHub App ID (App auth mode). |
| `GITHUB_INSTALLATION_ID` | — | GitHub App installation ID (App auth mode). |
| `GITHUB_SECRET_ID` | — | AWS Secrets Manager secret ID containing the App private key. |
| `GITHUB_API_BASE` | `https://api.github.com` | GitHub API base URL. Override for GitHub Enterprise. |
| `GITHUB_TOKEN_REFRESH_BUFFER` | `300` | Seconds before expiry at which to proactively refresh the installation token. |
| `GITHUB_JWT_EXPIRY` | `600` | JWT lifetime in seconds. |

---

## Confluence

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFLUENCE_SERVER_URL` | — | Confluence base URL (e.g., `https://myorg.atlassian.net/wiki`). |
| `CONFLUENCE_TOKEN` | — | Confluence API token. |
| `CONFLUENCE_SPACE_KEY` | — | Default space key. Used when `space_key` is omitted in tool calls. |
| `PARENT_PAGE_ID` | — | Default parent page ID for new pages. |

---

## Email / SMTP

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_SERVER` | — | SMTP server hostname. |
| `SMTP_PORT` | `25` | SMTP port. |
| `SMTP_SERVER_IP` | — | Optional fallback IP for the SMTP server. |
| `SMTP_USER` | — | SMTP username (authenticated mode only). |
| `SMTP_PASS` | — | SMTP password (authenticated mode only). |
| `SENDER_EMAIL` | — | From address for all outgoing email. |

---

## Messaging

### SQS

| Variable | Default | Description |
|----------|---------|-------------|
| `SQS_QUEUE_URL` | — | Full SQS queue URL. |
| `IS_EVALUATION` | `false` | Evaluation mode — skips actual send. |

### Webhook

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_URL` | — | Default webhook endpoint URL. |
| `WEBHOOK_AUTH_HEADER` | `X-Auth-Token` | Authentication header name. |
| `WEBHOOK_AUTH_TOKEN` | — | Authentication token value. |
| `WEBHOOK_TIMEOUT` | `30` | Request timeout in seconds. |
| `IS_EVALUATION` | `false` | Evaluation mode — skips actual send. |

---

## Storage (S3)

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_URL` | — | S3 URL or endpoint (e.g., `s3://my-bucket`). |
| `IS_EVALUATION` | `false` | Evaluation mode — skips actual upload. |

---

## Database

### DynamoDB

| Variable | Default | Description |
|----------|---------|-------------|
| `DYNAMODB_TABLE_NAME` | — | DynamoDB table name. |
| `DYNAMODB_PARTITION_KEY` | `session_id` | Partition key attribute name. |
| `DYNAMODB_SORT_KEY` | — | Sort key attribute name. Omit for tables with no sort key. |
| `DYNAMODB_ENDPOINT_URL` | — | Custom DynamoDB endpoint (for DynamoDB Local in testing). |
| `IS_EVALUATION` | `false` | Evaluation mode — skips actual write. |

### PostgreSQL

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_HOST` | — | Database host. |
| `POSTGRES_NAME` | — | Database name. |
| `POSTGRES_USERNAME` | — | Username. |
| `POSTGRES_PASSWORD` | — | Password. |
| `POSTGRES_PORT` | `5432` | Port. |
| `POSTGRES_TABLE` | — | Default table name. |
| `IS_EVALUATION` | `false` | Evaluation mode — skips actual write. |

---

## Compute (Lambda)

| Variable | Default | Description |
|----------|---------|-------------|
| `LAMBDA_FUNCTION_NAME` | — | Lambda function ARN or name. |
| `LAMBDA_INVOCATION_TYPE` | `RequestResponse` | Default invocation type. `RequestResponse` (sync) or `Event` (async). |
| `IS_EVALUATION` | `false` | Evaluation mode — skips actual invocation. |

---

## Agent Completions

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_API_ENDPOINT` | `http://localhost:8000` | Completions API base URL. |
| `AGENT_API_KEY` | — | API key for the completions endpoint. |
| `AGENT_API_TIMEOUT` | `300` | Request timeout in seconds. |

---

## AWS (Config reader)

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_CONFIG_FILE` | `~/.aws/config` | Path to AWS config file. |

---

## Evaluation mode

`IS_EVALUATION=true` is a shared flag recognized by messaging, storage, database, and compute integrations. When set, tools skip their actual external calls and return a simulated success response. Useful for testing agent logic without side effects.

| Variable | Integrations affected |
|----------|-----------------------|
| `IS_EVALUATION` | SQS, Webhook, S3, DynamoDB, PostgreSQL, Lambda |
