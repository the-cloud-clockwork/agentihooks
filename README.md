# agentihooks

[![Standalone](https://img.shields.io/badge/runs-standalone-brightgreen)](https://the-cloud-clock-work.github.io/agentihooks/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/blob/main/LICENSE)
[![CI](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://the-cloud-clock-work.github.io/agentihooks/)

Hook system and MCP tool server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Intercepts every lifecycle event (session start/end, tool use, prompts, stops), provides MCP tools for external services, and includes a **Token Control Layer** that monitors context window usage, truncates verbose output, deduplicates file reads, and warns before quota exhaustion.

> **Full documentation:** [the-cloud-clock-work.github.io/agentihooks](https://the-cloud-clock-work.github.io/agentihooks/)

## Architecture

```
Claude Code
  |
  |-- Hook Events (stdin JSON) --> python -m hooks --> hook_manager.py
  |     SessionStart, PreToolUse,       |
  |     PostToolUse, Stop, ...         |-- transcript logging
  |     (10 events total)              |-- tool error memory
  |                                    |-- token control layer
  |                                    |-- metrics parsing
  |
  |-- statusLine (native setting) --> python -m hooks.statusline
  |     pipes JSON on every turn       --> 2-3 line status bar (ctx%, burn rate, cost, git)
  |
  +-- MCP Tools --> python -m hooks.mcp --> category modules
        aws, email, messaging,               --> hooks/integrations/*
        database, compute, ...
```

## Quick Start

**Requirement:** [uv](https://docs.astral.sh/uv/getting-started/installation/) must be installed.

```bash
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
cd agentihooks

# 1. Create the dedicated venv and install everything
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"

# 2. Install hooks + settings + MCP into ~/.claude
agentihooks init
```

`agentihooks init` wires hooks into `~/.claude/settings.json`, symlinks skills/agents/commands/rules, merges MCP servers into `~/.claude.json`, installs the CLI globally, and auto-starts background daemons. Every hook command is written with the Python that ran the installer, so all subprocesses find the right packages regardless of which shell or venv is active.

Re-run any time -- it is idempotent. See [Installation](https://the-cloud-clock-work.github.io/agentihooks/docs/getting-started/installation/) for a full walkthrough.

## CLI

```bash
# Install / re-install
agentihooks init                             # global install with default profile
agentihooks init --bundle ~/my-tools         # first-time: link bundle + install
agentihooks init --profile coding            # use a specific profile
agentihooks init --repo /path/to/repo        # per-repo config

# Launch claude with profile flags
agentihooks claude                           # reads profile.yml -> CLI flags
agenti                                       # alias (after source ~/.bashrc)

# Uninstall
agentihooks uninstall [--yes]                # remove everything

# Multi-account quota watcher
agentihooks quota auth <name>                # add/update account
agentihooks quota list                       # show all accounts
agentihooks quota switch [name]              # switch active account
agentihooks quota restart                    # stop + start daemon
agentihooks quota status                     # show current usage
agentihooks quota logs                       # tail daemon log
agentihooks quota stop                       # kill daemon
agentihooks quota remove <name>              # delete account

# Sync daemon (auto-propagation)
agentihooks daemon start                     # start background daemon (60s poll)
agentihooks daemon status                    # show targets + watched files
agentihooks daemon logs                      # tail daemon log
agentihooks daemon stop                      # kill daemon

# Status & diagnostics
agentihooks status                           # full system health + MCP fleet + guardrails + quota

# Token optimization
agentihooks lint-claude [path]               # analyze CLAUDE.md token cost
agentihooks extract-skill "Section" --name x # extract section to on-demand skill
agentihooks mcp report                       # MCP surface area report

# Utilities
agentihooks ignore [path] [--force]          # create .claudeignore
agentihooks --list-profiles                  # show available profiles
agentihooks --query                          # print active profile name
```

CLI output uses colored markers: `[OK]` green, `[--]` dim, `[!!]` yellow, `[RM]` red.

## What `init` Does

1. Links bundle (if `--bundle` provided)
2. Merges settings: `settings.base.json` -> profile `settings.overrides.json` -> OTEL config
3. Symlinks skills, agents, commands, and rules (3-layer merge)
4. Symlinks `CLAUDE.md` from profile root to `~/.claude/CLAUDE.md`
5. Installs MCP servers (hooks-utils + bundle + profile)
6. Applies MCP blacklist to all registered projects
7. Installs CLI globally via `uv tool`
8. Auto-starts quota daemon (if accounts exist) and sync daemon
9. Writes bashrc block (`agentienv` shell function + `agenti` alias)

## Profiles

Profiles mirror the Claude Code project structure:

```
profiles/<name>/
|-- CLAUDE.md                    # system prompt (-> ~/.claude/CLAUDE.md)
|-- profile.yml                  # agentihooks metadata + claude flags
+-- .claude/
    |-- settings.overrides.json  # merged into ~/.claude/settings.json
    |-- .mcp.json                # profile MCP servers
    |-- skills/                  # -> ~/.claude/skills/
    |-- agents/                  # -> ~/.claude/agents/
    |-- commands/                # -> ~/.claude/commands/
    +-- rules/                   # -> ~/.claude/rules/
```

Built-in profiles: `default`, `coding`, `admin`. Bundle profiles are discovered automatically.

**3-layer merge:** agentihooks built-in -> bundle global `.claude/` -> profile-specific `.claude/`. Applies to skills, agents, commands, rules, and MCP servers.

Switch profiles: `agentihooks init --profile <name>`. List all: `agentihooks --list-profiles`.

### `agentihooks claude`

Reads the `claude:` section from the active profile's `profile.yml` and maps fields to CLI flags:

| profile.yml field | CLI flag |
|---|---|
| `permission_mode: bypassPermissions` | `--dangerously-skip-permissions` |
| `model: sonnet` | `--model sonnet` |
| `effort: high` | `--effort high` |
| `worktree: true` | `--worktree` |

Also loads env vars from `~/.agentihooks/.env` and companion `*.env` files before exec. Extra arguments are passed through: `agentihooks claude --verbose`.

## Bundles

A **bundle** is an external directory containing custom profiles and shared assets. Link it once; everything is auto-discovered.

```
my-bundle/
|-- .claude/                     # global assets shared by all profiles
|   |-- skills/
|   |-- agents/
|   |-- commands/
|   |-- rules/
|   +-- .mcp.json                # bundle-wide MCP servers
+-- profiles/
    +-- infra-ops/               # custom profile
        |-- CLAUDE.md
        |-- profile.yml
        +-- .claude/
            +-- settings.overrides.json
```

```bash
agentihooks init --bundle ~/dev/my-bundle    # link bundle + install
agentihooks --list-profiles                  # shows built-in + bundle profiles
agentihooks init --profile infra-ops         # use a bundle profile
```

## Hook Events

10 lifecycle events, all handled by `python -m hooks`:

| Event | What happens |
|-------|-------------|
| `SessionStart` | Injects session awareness, MCP hygiene reminder, MCP surface area warning |
| `PreToolUse` | Secret scan (blocks on detection), file read deduplication, CLAUDE.md sanity check, tool error memory |
| `PostToolUse` | Truncates verbose bash output, marks files read, records tool errors |
| `Stop` | Scans transcript for errors, parses metrics, auto-saves memory |
| `SessionEnd` | Logs transcript, clears file read cache |
| `SubagentStop` | Logs subagent transcript |
| `UserPromptSubmit` | Warns on detected secrets |
| `Notification` | Logs notifications |
| `PreCompact` | Logs before context compaction |
| `PermissionRequest` | Logs permission requests |

**StatusLine** is not a hook event -- it is a native Claude Code setting handled by `hooks/statusline.py`. Emits a 2-3 line terminal status bar with context fill %, burn rate, cost, cache ratio, git branch, and quota.

## Multi-Account Quota

The quota watcher is a headless Playwright daemon that scrapes claude.ai/settings/usage and writes JSON for the statusline. Supports multiple accounts.

```bash
# One-time: install Playwright's browser
~/.agentihooks/.venv/bin/python -m playwright install chromium

# Add accounts
agentihooks quota auth personal     # opens browser, prompts for sessionKey
agentihooks quota auth team         # add another account

# Manage
agentihooks quota list              # show all, mark active
agentihooks quota switch team       # switch + restart daemon
agentihooks quota remove personal   # delete account
```

Accounts are stored in `~/.agentihooks/quota-accounts/<name>.json`. Enable in `~/.agentihooks/.env`:

```bash
CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json
```

Statusline output example:
```
session:53% [1h] | all:35% resets fri 10:00 am | sonnet:5% resets mon 12:00 am
```

## Sync Daemon

`scripts/sync_daemon.py` watches 27+ source files across all 3 layers (built-in, bundle, profile) and auto-propagates changes to every registered target within one poll cycle. Additive only -- never deletes user files.

```bash
agentihooks daemon start            # start (60s default poll)
agentihooks daemon status           # show targets + watched files
agentihooks daemon stop             # kill daemon
```

| Source change | Affected targets |
|---|---|
| `settings.base.json` | Global + ALL projects |
| `profiles/{X}/*` | Targets using profile X |
| `.env` files | Global + ALL projects |
| Bundle directory | Global + ALL projects |

An advisory file lock (`~/.agentihooks/sync.lock`) prevents concurrent writes between the daemon and manual `agentihooks init` commands.

## Status & Diagnostics

`agentihooks status` validates your entire installation and shows the real state of your MCP fleet:

```
[OK] Profile: colt (bundle: ~/dev/agentihooks-bundle)
[OK] Hooks: 10/10 wired in settings.json
[OK] Python: uv/tools/agentihooks/bin/python3 (Python 3.11.15)
[OK] Daemons: sync (PID 1234), quota (PID 5678)
[OK] Redis: connected — 3568 keys (memory: 3540, file_cache: 14)
[OK] OTEL: enabled
[OK] Cost guardrails: 7/7 active
     + bash_filter: Truncates verbose bash output
     + file_dedup: Blocks re-reading unchanged files
     + context_audit: Tracks per-tool token consumption
     + effort_policy: Thinking/effort guidance, expensive subagent warnings
     + peak_hours: Peak billing indicator on statusline
     + compact_suggest: Smart /compact suggestions from audit data
     + claude_md_sanity: Blocks CLAUDE.md edits exceeding 200 lines
[OK] MCP: 9 servers (all disabled here) — 450 tools total, 0 active here
     - hooks-utils [stdio] (25 tools)
     - gateway-core [http] (93 tools)
     - gateway-infra [http] (131 tools)
     ...
```

The MCP check reads `~/.claude.json` for server configs, resolves `${ENV_VAR}` auth from your shell environment, queries each HTTP server via MCP protocol (`initialize` + `tools/list`) for real tool counts, and caches results for 1 hour at `~/.agentihooks/mcp-tool-cache.json`. Per-project blacklists are read from `~/.claude.json` projects block.

**In-session skill:** Use `/agentihooks` inside Claude Code to see the same diagnostics plus live session metrics (context fill, burn rate, per-tool consumption from the context audit).

## MCP Tool Categories

Tools exposed by the `hooks-utils` MCP server, selectively loaded via `MCP_CATEGORIES`:

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

## Configuration

All configuration goes in `.env` files in `~/.agentihooks/`. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_HOME` | `~/.agentihooks` | Root for logs, memory, and state |
| `MCP_CATEGORIES` | `all` | Comma-separated tool categories to load |
| `TOKEN_CONTROL_ENABLED` | `true` | Master switch for the token control layer |
| `TOKEN_WARN_PCT` | `60` | Context fill % that triggers a warning |
| `TOKEN_CRITICAL_PCT` | `80` | Context fill % that triggers a critical banner |
| `BASH_FILTER_ENABLED` | `true` | Truncate verbose bash output |
| `FILE_READ_CACHE_ENABLED` | `true` | Block redundant file re-reads |
| `CONTEXT_AUDIT_ENABLED` | `true` | Track per-tool token consumption across sessions |
| `EFFORT_POLICY_ENABLED` | `true` | Inject thinking/effort guidance at session start |
| `DEFAULT_EFFORT` | `medium` | Default reasoning effort (low/medium/high) |
| `PEAK_HOURS_ENABLED` | `true` | Show peak/off-peak billing indicator |
| `COMPACT_SUGGEST_ENABLED` | `true` | Smart /compact suggestions using audit data |
| `AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK` | `true` | Block edits that would bloat CLAUDE.md past line limit |
| `AGENTIHOOKS_CLAUDE_MD_MAXLINES` | `200` | Max allowed lines in CLAUDE.md / CLAUDE.local.md |
| `REDIS_URL` | -- | Redis connection string (graceful degradation when unavailable) |
| `CLAUDE_USAGE_FILE` | -- | Path to quota JSON (enables statusline quota display) |
| `CLAUDE_USAGE_POLL_SEC` | `60` | Quota watcher poll interval |
| `AGENTIHOOKS_SYNC_POLL_SEC` | `60` | Sync daemon poll interval |
| `ENABLE_TOOL_SEARCH` | `true` | Lazy-load MCP tools on demand |

Complete table: [Configuration Reference](https://the-cloud-clock-work.github.io/agentihooks/docs/reference/configuration/)

## Portability

Everything user-specific lives in `~/.agentihooks/`:

```
~/.agentihooks/
|-- .venv/                     # canonical Python venv
|-- .env                       # main env vars (loaded first)
|-- *.env                      # companion env files (auto-sourced)
|-- state.json                 # install state, targets, active profile
|-- logs/                      # hook + daemon logs
|-- memory/                    # cross-session agent memory
|-- quota-accounts/            # multi-account auth state
|   +-- <name>.json
|-- claude_usage.json          # written by quota daemon, read by statusline
|-- sync-daemon.pid            # sync daemon PID
|-- sync-hashes.json           # daemon file hashes
|-- sync.lock                  # advisory lock
+-- mcp-tool-cache.json        # cached MCP tool counts (1h TTL, auto-refreshed)
```

To move to a new machine: clone the repo, copy `~/.agentihooks/.env`, recreate the venv, run the installer:

```bash
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"
agentihooks init
```

## Per-Repo Config

```bash
agentihooks init --repo /path/to/repo    # per-repo config with profile picker
```

This writes `.claude/settings.local.json` in the target repo -- the highest-priority settings file in Claude Code. It merges the profile's permissions and MCP overrides into the repo scope.

## Extending

Add a new MCP tool category with a `register(server)` function + one line in `_registry.py`. Add a new hook handler with one function + one entry in the dispatcher dict.

Guide: [Extending AgentiHooks](https://the-cloud-clock-work.github.io/agentihooks/docs/extending/)

## Related Projects

| Project | Description |
|---------|-------------|
| [agenticore](https://github.com/The-Cloud-Clock-Work/agenticore) | Claude Code runner and orchestrator |
| [agentibridge](https://github.com/The-Cloud-Clock-Work/agentibridge) | MCP server for session persistence and remote control |

## License

See [LICENSE](LICENSE) for details.
