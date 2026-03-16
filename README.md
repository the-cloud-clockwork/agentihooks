# agentihooks

[![Standalone](https://img.shields.io/badge/runs-standalone-brightgreen)](https://the-cloud-clock-work.github.io/agentihooks/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/blob/main/LICENSE)
[![CI](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://the-cloud-clock-work.github.io/agentihooks/)

Hook system and MCP tool server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents. Designed to work with [agenticore](https://github.com/The-Cloud-Clock-Work/agenticore) and meant to be forked and extended for custom workflows.

**agentihooks** intercepts every Claude Code lifecycle event (session start/end, tool use, prompts, stops) and provides 45 MCP tools across 12 categories for interacting with external services. A built-in **Token Control Layer** monitors context window usage, truncates verbose command output, deduplicates file reads, and warns before quota exhaustion — targeting 30–50% token reduction in agentic sessions.

> **Full documentation:** [the-cloud-clock-work.github.io/agentihooks](https://the-cloud-clock-work.github.io/agentihooks/)

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
  └── MCP Tools ──► python -m hooks.mcp ──► 12 category modules
        github, aws, confluence,              │
        email, database, ...                  └── hooks/integrations/*
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
| `SessionStart` | Creates session context directory, injects session awareness, MCP hygiene reminder |
| `PreToolUse` | Secret scan (blocks on detection), file read deduplication, injects tool error memory |
| `PostToolUse` | Truncates verbose bash output, marks files read, records tool errors |
| `Stop` | Scans transcript for errors, parses metrics, auto-saves memory |
| `SessionEnd` | Logs transcript, clears file read cache, cleans up session directory |
| `SubagentStop` | Logs subagent transcript |
| `UserPromptSubmit` | Warns on detected secrets |
| `Notification` | Logs notifications |
| `PreCompact` | Logs before context compaction |
| `PermissionRequest` | Logs permission requests |

**StatusLine** is not a hook event — it is a native Claude Code setting (`"statusLine"` key in `settings.json`) handled by `hooks/statusline.py`. It emits a 2–3 line terminal status bar with context fill %, burn rate, cost, cache ratio, and git branch on every turn. `used_pct` is recomputed from `total_input_tokens / context_window_size * 100` to avoid stale payload values.

Full payload schemas and handler details: [Hook Events](https://the-cloud-clock-work.github.io/agentihooks/docs/hooks/events/)

## MCP Tool Categories

45 tools across 12 categories, selectively loaded via `MCP_CATEGORIES`:

| Category | Tools | Description |
|----------|------:|-------------|
| `github` | 5 | Clone repos, create PRs, token management, git summary |
| `confluence` | 9 | CRUD pages, markdown docgen, validation |
| `aws` | 4 | Profile listing, account discovery |
| `email` | 2 | SMTP send with text / HTML / markdown |
| `messaging` | 3 | SQS + webhook with state enrichment |
| `storage` | 2 | S3 upload, `/tmp`-restricted filesystem delete |
| `database` | 3 | DynamoDB put, PostgreSQL insert + execute |
| `compute` | 1 | Lambda invocation (sync/async) |
| `observability` | 7 | Timers, metrics, structured logging, container log tailing |
| `smith` | 4 | Command builder: list, prompt, build, execute |
| `agent` | 1 | Remote agent completions with model presets |
| `utilities` | 4 | Mermaid validation, markdown writer, env vars, tool listing |

Per-tool signatures, parameters, and environment variables: [MCP Tools](https://the-cloud-clock-work.github.io/agentihooks/docs/mcp-tools/)

## CLI

```bash
agentihooks global [--profile <name>]   # install/re-apply to ~/.claude
agentihooks project <path>              # write .mcp.json into a project
agentihooks mcp                         # list MCP files in ~/.agentihooks/
agentihooks mcp install                 # two-stage: pick file → pick servers
agentihooks mcp uninstall               # two-stage: pick file → pick servers to remove
agentihooks mcp add <path>              # install a file directly by path
agentihooks mcp sync                    # re-apply all installed MCP files
agentihooks ignore [path] [--force]     # create .claudeignore in cwd (or given path)
agentihooks uninstall                   # remove everything
agentihooks --loadenv                   # install agentienv shell function into ~/.bashrc
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

Complete table covering all 50+ variables across every integration: [Configuration Reference](https://the-cloud-clock-work.github.io/agentihooks/docs/reference/configuration/)

## Profiles

Profiles bundle a system prompt (`CLAUDE.md`), MCP category selection, and model settings. Switch with `agentihooks global --profile <name>`.

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
├── memory/                    # Cross-session agent memory
├── playwright_profile/        # Persistent browser auth for quota watcher (created on first --headed run)
└── claude_usage.json          # Written by quota watcher; read by statusline (optional)
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
