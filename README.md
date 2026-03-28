# agentihooks

[![Standalone](https://img.shields.io/badge/runs-standalone-brightgreen)](https://the-cloud-clock-work.github.io/agentihooks/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/blob/main/LICENSE)
[![CI](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://the-cloud-clock-work.github.io/agentihooks/)

Hook system and MCP tool server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents. Designed to work with [agenticore](https://github.com/The-Cloud-Clock-Work/agenticore) and meant to be forked and extended for custom workflows.

**agentihooks** intercepts every Claude Code lifecycle event (session start/end, tool use, prompts, stops) and provides 26 MCP tools across 8 categories for interacting with external services. A built-in **Token Control Layer** monitors context window usage, truncates verbose command output, deduplicates file reads, and warns before quota exhaustion — targeting 30–50% token reduction in agentic sessions.

> **Full documentation:** [the-cloud-clock-work.github.io/agentihooks](https://the-cloud-clock-work.github.io/agentihooks/)
>
> **Cost management guide:** [the-cloud-clock-work.github.io/agentihooks/docs/cost-management/](https://the-cloud-clock-work.github.io/agentihooks/docs/cost-management/) — see how AgentiHooks saves 100K–250K tokens per session

## Architecture

```
Claude Code
  │
  ├── Hook Events (stdin JSON) ──► python -m hooks ──► hook_manager.py
  │     SessionStart, PreToolUse,       │
  │     PostToolUse, Stop, ...          ├── transcript logging
  │     (10 events total)               ├── tool error memory
  │                                     ├── token control layer
  │                                     ├── metrics parsing
  │                                     └── email notifications
  │
  ├── statusLine (native setting) ──► python -m hooks.statusline
  │     pipes JSON on every turn        └── 2-3 line status bar (ctx%, burn rate, cost, git)
  │
  └── MCP Tools ──► python -m hooks.mcp ──► 8 category modules
        aws, email, messaging,                │
        database, compute, ...                └── hooks/integrations/*
```

## Quick Start

**Requirement:** [uv](https://docs.astral.sh/uv/getting-started/installation/) must be installed.

```bash
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
cd agentihooks

# 1. Create the dedicated venv at ~/.agentihooks/.venv and install everything
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"

# 2. Install hooks + settings + MCP into ~/.claude
~/.agentihooks/.venv/bin/python scripts/install.py global
```

`agentihooks global` wires hooks into `~/.claude/settings.json`, symlinks skills/agents, merges MCP servers into `~/.claude.json`, and installs the CLI globally. **Critically, every hook command in `settings.json` is written with the Python that ran the installer** — so by running the installer from `~/.agentihooks/.venv`, all hook subprocesses find the right packages regardless of which shell or virtual environment is active when Claude Code fires them.

Re-run any time — it's idempotent. See [Installation](https://the-cloud-clock-work.github.io/agentihooks/docs/getting-started/installation/) for the full step-by-step walkthrough including Redis setup and quota display.

## Hook Events

10 lifecycle events, all handled by `python -m hooks`:

| Event | What happens |
|-------|-------------|
| `SessionStart` | Injects session awareness, MCP hygiene reminder |
| `PreToolUse` | Secret scan (blocks on detection), file read deduplication, injects tool error memory |
| `PostToolUse` | Truncates verbose bash output, marks files read, records tool errors |
| `Stop` | Scans transcript for errors, parses metrics, auto-saves memory |
| `SessionEnd` | Logs transcript, clears file read cache |
| `SubagentStop` | Logs subagent transcript |
| `UserPromptSubmit` | Warns on detected secrets |
| `Notification` | Logs notifications |
| `PreCompact` | Logs before context compaction |
| `PermissionRequest` | Logs permission requests |

**StatusLine** is not a hook event — it is a native Claude Code setting (`"statusLine"` key in `settings.json`) handled by `hooks/statusline.py`. It emits a 2–3 line terminal status bar with context fill %, burn rate, cost, cache ratio, and git branch on every turn. `used_pct` is recomputed from `total_input_tokens / context_window_size * 100` to avoid stale payload values.

Full payload schemas and handler details: [Hook Events](https://the-cloud-clock-work.github.io/agentihooks/docs/hooks/events/)

## MCP Tool Categories

26 tools across 8 categories, selectively loaded via `MCP_CATEGORIES`:

| Category | Tools | Description |
|----------|------:|-------------|
| `aws` | 4 | Profile listing, account discovery |
| `email` | 2 | SMTP send with text / HTML / markdown |
| `messaging` | 3 | SQS + webhook with state enrichment |
| `storage` | 1 | S3 upload |
| `database` | 3 | DynamoDB put, PostgreSQL insert + execute |
| `compute` | 1 | Lambda invocation (sync/async) |
| `observability` | 7 | Timers, metrics, structured logging, container log tailing |
| `utilities` | 3 | Markdown writer, env vars, tool listing |

Per-tool signatures, parameters, and environment variables: [MCP Tools](https://the-cloud-clock-work.github.io/agentihooks/docs/mcp-tools/)

## CLI

```bash
# Core
agentihooks global [--profile <name>]   # install/re-apply to ~/.claude
agentihooks init [--profile <name>]     # set up per-repo config → .claude/settings.local.json
agentihooks --list-profiles             # show all profiles (built-in + bundle)
agentihooks --query                     # print active profile name

# Bundle (external profiles + connectors in one directory)
agentihooks bundle link <path>          # link a bundle directory
agentihooks bundle list                 # show bundle contents
agentihooks bundle unlink               # remove bundle

# Connectors (MCP deny rules + env vars per profile)
agentihooks connector list              # list linked connectors
agentihooks connector link <path>       # link a connector directory
agentihooks connector unlink <name>     # unlink by name
agentihooks connector inspect <path>    # preview what a connector would merge
agentihooks connector new               # interactive scaffold
agentihooks connector new --name x ...  # headless scaffold (for agents/scripts)

# MCP server files
agentihooks mcp                         # list MCP files in ~/.agentihooks/
agentihooks mcp install                 # pick file → pick servers
agentihooks mcp uninstall               # pick file → remove servers
agentihooks mcp add <path>              # install a file directly by path
agentihooks mcp sync                    # re-apply all installed MCP files

# Project
agentihooks project <path>              # write .mcp.json into a project
agentihooks ignore [path] [--force]     # create .claudeignore

# Quota
agentihooks quota [auth|status|logs|stop]

# Utilities
agentihooks uninstall                   # remove everything
agentihooks --loadenv                   # load ~/.agentihooks/.env into shell
```

Full reference: [CLI Commands](https://the-cloud-clock-work.github.io/agentihooks/docs/reference/cli-commands/)

## Configuration

All integrations are configured via environment variables. Key ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_HOME` | `~/.agentihooks` | Root for logs, memory, and state |
| `CLAUDE_CODE_HOME_DIR` | `$HOME` | Home-directory root override (`.claude` appended automatically) |
| `AGENTIHOOKS_CLAUDE_HOME` | `~/.claude` | Legacy: direct path to `.claude` directory |
| `AGENTIHOOKS_PROFILE` | `default` | Profile to use for `agentihooks global` / `project` (env alternative to `--profile`) |
| `AGENTIHOOKS_MCP_FILE` | — | Path to an MCP JSON file to auto-merge during `agentihooks global` |
| `MCP_CATEGORIES` | `all` | Comma-separated list of tool categories to load |
| `LOG_ENABLED` | `true` | Enable hook logging |
| `MEMORY_AUTO_SAVE` | `true` | Auto-save session digest on Stop |
| `REDIS_URL` | — | Redis connection string — format: `redis://:PASSWORD@host:port/db`. Used by token monitor (burn rate), file read cache (dedup), and warning edge-triggers. Graceful degradation when unavailable. |
| `TOKEN_CONTROL_ENABLED` | `true` | Master switch for the token control layer |
| `TOKEN_WARN_PCT` | `60` | Context fill % that triggers a warning injection |
| `TOKEN_CRITICAL_PCT` | `80` | Context fill % that triggers a critical banner |
| `BASH_FILTER_ENABLED` | `true` | Truncate verbose bash output (docker logs, git log, etc.) |
| `FILE_READ_CACHE_ENABLED` | `true` | Block redundant file re-reads within a session |
| `MCP_HYGIENE_ENABLED` | `true` | Inject MCP server usage reminder at session start |
| `ENABLE_TOOL_SEARCH` | `true` | Make all MCP tools lazy-loaded on demand (set in `env` block of `settings.json`); eliminates ~79K token upfront cost from MCP tool schemas |
| `CLAUDE_USAGE_FILE` | — | Path to quota JSON file (e.g. `~/.agentihooks/claude_usage.json`). Must be set to enable statusline line 3 quota display. |
| `CLAUDE_USAGE_STALE_SEC` | `300` | Quota data older than this (seconds) shows "stale" on statusline |
| `CLAUDE_USAGE_POLL_SEC` | `60` | Quota watcher daemon poll interval (seconds) |

Complete table covering all 50+ variables across every integration: [Configuration Reference](https://the-cloud-clock-work.github.io/agentihooks/docs/reference/configuration/)

## Profiles

Profiles bundle permissions, env vars, a system prompt (`CLAUDE.md`), and MCP settings. Each profile defines a permission tier:

| Profile | Mode | Deny | Ask | Best for |
|---------|------|------|-----|----------|
| **default** | `default` | — | git push, rm -rf, docker rm, kubectl delete | Daily development |
| **coding** | `acceptEdits` | Protected branch pushes, git merge, gh CLI | git push, rm -rf, docker, kubectl | Focused coding, CI agents |
| **admin** | `auto` | — | force push, rm -rf / | Infrastructure ops |

Permission evaluation order: **deny > ask > allow** (first match wins).

Switch profiles: `agentihooks global --profile <name>`. Per-repo override: `agentihooks init --profile <name>`.

Profiles come from two sources:
- **Built-in** — `profiles/` in the agentihooks repo (default, coding, admin)
- **Bundle** — external directory linked via `agentihooks bundle link`

`agentihooks --list-profiles` shows both.

## Bundles

A **bundle** is a single external directory containing all your personal customizations — custom profiles, connectors, and MCP configs. Link it once and everything is auto-discovered.

```
my-tools/.agentihooks/          ← this IS the bundle
  profiles/
    infra-ops/                   # custom profile
      profile.yml
      settings.overrides.json
      .claude/CLAUDE.md
  connectors/
    my-mcp-filter/               # MCP deny rules per profile
      connector.yml
      profiles/default/permissions.json
      profiles/coding/permissions.json
```

```bash
agentihooks bundle link ~/dev/my-tools/.agentihooks   # link once
agentihooks --list-profiles                           # shows built-in + bundle
agentihooks global --profile infra-ops                # use a bundle profile
```

Connectors inside the bundle are auto-linked — no separate `connector link` needed. Full docs: [Bundles](docs/bundles.md), [Connectors](docs/connectors.md).

## Per-Repo Config

Drop a `.agentihooks.json` in any repo to override the global profile:

```json
{
  "profile": "coding",
  "disabledMcpServers": ["gateway-media"],
  "permissions": { "deny": ["Bash(terraform apply *)"] }
}
```

```bash
agentihooks init --profile coding   # creates .agentihooks.json + .claude/settings.local.json
```

This writes `.claude/settings.local.json` — the highest-priority user settings file in Claude Code. It merges the profile's permissions + connector rules + your repo overrides. Run once per repo; re-run after changing connectors or profiles.

## Portability

Everything user-specific lives in `~/.agentihooks/`:

```
~/.agentihooks/
├── .venv/                     # Canonical Python venv — all hook subprocesses run from here
├── .env                       # Main credentials (seeded from .env.example, loaded first)
├── *.env                      # Companion env files (auto-sourced after .env)
├── *.json                     # Drop MCP server files here → agentihooks mcp install
├── state.json                 # Tracked MCP files and other state
├── logs/                      # Hook + MCP logs
│   └── quota-watcher.log      # Quota watcher daemon log
├── memory/                    # Cross-session agent memory
├── claude_auth_state.json     # Playwright storage state (sessionKey cookie for quota watcher)
├── claude_usage.json          # Written by quota watcher daemon; read by statusline (optional)
└── quota-watcher.pid          # Daemon PID file
```

To move to a new machine: clone the repo, copy `~/.agentihooks/.env`, recreate the venv, run the installer. Done:

```bash
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"
~/.agentihooks/.venv/bin/python scripts/install.py global
```

**Install the `agentienv` shell function** (sources `.env` into any shell on demand — also auto-called on every new shell):

```bash
agentihooks --loadenv   # writes managed block to ~/.bashrc
source ~/.bashrc
agentienv          # load vars into current shell before launching claude
```

**Manage MCP server files** — drop `.json` files into `~/.agentihooks/`:

```bash
agentihooks mcp             # list available MCP files
agentihooks mcp install     # interactive: pick one to install
agentihooks mcp uninstall   # interactive: pick one to remove
agentihooks mcp add <path>  # install directly by path
```

Details: [Portability & Reusability](https://the-cloud-clock-work.github.io/agentihooks/docs/getting-started/portability/)

## Extending

Add a new MCP tool category with a `register(server)` function + one line in `_registry.py`. Add a new hook handler with one function + one entry in the dispatcher dict.

Guide: [Extending AgentiHooks](https://the-cloud-clock-work.github.io/agentihooks/docs/extending/)

## Code Quality

Continuously analyzed by [SonarQube](https://sonar.homeofanton.com/dashboard?id=agentihooks).

## Related Projects

| Project | Description |
|---------|-------------|
| [agenticore](https://github.com/The-Cloud-Clock-Work/agenticore) | Claude Code runner and orchestrator |
| [agentibridge](https://github.com/The-Cloud-Clock-Work/agentibridge) | MCP server for session persistence and remote control |
| [agentihub](https://github.com/The-Cloud-Clock-Work/agentihub) (private) | Agent identities — CLAUDE.md, workflows, evaluation. Provisioned directly by agenticore. |

## License

See [LICENSE](LICENSE) for details.
