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
agentihooks init --local                     # per-repo config for current directory
agentihooks init --local --profile coding    # override profile for current project

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

# Bundle management
agentihooks bundle list                      # show linked bundle + profiles
agentihooks bundle pull                      # git pull the linked bundle
agentihooks bundle pull --rebase             # git pull --rebase
agentihooks bundle link ~/dev/my-tools       # link a bundle directory
agentihooks bundle unlink                    # unlink current bundle

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
2. Merges settings: `settings.base.json` -> each profile's `settings.overrides.json` (chained) -> OTEL config
3. Symlinks skills, agents, commands, and rules (3-layer merge, additive across chain)
4. Writes `CLAUDE.md` to `~/.claude/CLAUDE.md` (copy for single profile, concatenated for chains)
5. Installs MCP servers (hooks-utils + bundle + profile)
6. Applies hierarchy-aware MCP blacklist to all registered projects (respects per-project `.agentihooks.json` whitelists)
7. Prunes orphaned MCP servers from `~/.claude.json` (servers no longer defined in any source)
8. Installs CLI globally via `uv tool`
9. Restarts sync daemon (always — picks up code changes)
10. Auto-starts quota daemon (if accounts exist)
11. Writes bashrc block (`agentienv` shell function + `agenti` alias)

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

Switch profiles: `agentihooks init --profile <name>`. Chain profiles: `agentihooks init --profile coding,colt`. List all: `agentihooks --list-profiles`.

**Profile chaining:** Comma-separated profiles are applied left to right. Settings deep-merge sequentially (hooks append), rules/skills/agents/commands accumulate additively, CLAUDE.md files are concatenated into one file with `---` separators. Query: `agentihooks --query` shows `chain: [coding, colt]`.

**Settings profiles (two-axis model):** Independently control the settings layer without changing rules or CLAUDE.md:

```bash
agentihooks init --profile colt --settings-profile admin   # persona + settings overlay
agentihooks settings-profile admin                          # quick-switch settings only
agentihooks settings-profile --clear                        # revert to persona defaults
```

The settings profile applies only `settings.overrides.json` and `.mcp.json` — rules, CLAUDE.md, skills, agents, and commands come from the persona profile. Env var: `AGENTIHOOKS_SETTINGS_PROFILE`.

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

## Entity Merge Behavior

AgentiHooks entities (rules, skills, agents, commands, settings, MCP servers, CLAUDE.md) are installed through a 3-layer merge: **agentihooks built-in -> bundle global -> profile-specific**. Each entity type has different merge semantics:

| Entity | Merge type | Same-name collision | Notes |
|--------|-----------|---------------------|-------|
| **Rules** (`rules/*.md`) | Additive | Later layer overwrites symlink | All layers' unique files coexist in `~/.claude/rules/` |
| **Skills** (`skills/`) | Additive | Later layer overwrites symlink | Directory-based; same directory name = override |
| **Agents** (`agents/*.md`) | Additive | Later layer overwrites symlink | Same filename = profile version wins |
| **Commands** (`commands/*.md`) | Additive | Later layer overwrites symlink | Same filename = profile version wins |
| **Settings** (`settings.json`) | Deep merge | Dicts merge, hook arrays append, other arrays replace | See key-by-key table below |
| **MCP servers** (`.mcp.json`) | Additive | Same server name = later layer overwrites | Different server names accumulate |
| **CLAUDE.md** | Copy (single) / Concatenate (chain) | Last profile wins (single); all profiles merged (chain) | Written as a real file, not a symlink (WSL/Windows compatible) |
| **.env files** | Load order | Later file overrides same key | `~/.agentihooks/.env` first, then `*.env` alphabetically |

**Key implication for rules:** If your bundle defines `rules/python-files.md` and your profile also defines `rules/python-files.md`, the profile version wins (Layer 3 re-links over Layer 2). To add rules without overriding, use unique filenames. All rules from all layers with distinct names coexist in `~/.claude/rules/`.

### Settings.json key-by-key merge reference

`settings.base.json` is the source of truth. Running `agentihooks init` or the sync daemon deep-merges profile/bundle `settings.overrides.json` on top. The merge behavior depends on the **type** of each key:

| Key | Type | Merge behavior | Safe to override? |
|-----|------|----------------|-------------------|
| `autoUpdatesChannel` | string | Replaced | Yes |
| `skipDangerousModePermissionPrompt` | bool | Replaced | Yes |
| `model` | string | Replaced | Yes — common profile override |
| `env` | dict | **Key-by-key merge** — new keys added, existing keys overwritten, unmentioned keys kept | Yes — add or override env vars freely |
| `permissions` | dict | **Key-by-key merge** at dict level | Partially |
| `permissions.allow` | **array** | **Replaced entirely** — profile's array is the full list | Yes — define the complete permissions you want |
| `statusLine` | dict | **Key-by-key merge** | Not recommended |
| `hooks` | dict | **Key-by-key merge** at the dict level | Yes |
| `hooks.PreToolUse` (etc.) | **array** | **Appended** — profile hooks added after base hooks | Yes — only define your extra hooks, base hooks are preserved |

**The rule:** Dicts merge (keys combine). Hook arrays append (profile hooks are added after base hooks). All other arrays replace (profile's list wins). Profiles only need to define what's unique to them — base hooks are always preserved.

## Hook Events

10 lifecycle events, all handled by `python -m hooks`:

| Event | What happens |
|-------|-------------|
| `SessionStart` | Injects session awareness, MCP hygiene reminder, MCP surface area warning |
| `PreToolUse` | Secret scan (blocks on detection), file read deduplication, CLAUDE.md sanity check, tool error memory |
| `PostToolUse` | Truncates verbose bash output, marks files read, records tool errors |
| `Stop` | Scans transcript for errors, parses metrics, auto-saves memory |
| `SessionEnd` | Logs transcript, clears file read cache, clears context refresh state |
| `SubagentStop` | Logs subagent transcript |
| `UserPromptSubmit` | Warns on detected secrets, context refresh (rules re-injection every N turns) |
| `Notification` | Logs notifications |
| `PreCompact` | Logs before context compaction |
| `PermissionRequest` | Logs permission requests |

**StatusLine** is not a hook event -- it is a native Claude Code setting handled by `hooks/statusline.py`. Emits a 2-3 line terminal status bar with context fill %, burn rate, cost, cache ratio, git branch, and quota.

## Context Preprocessor — Token Compression

AgentiHooks includes a built-in **Context Preprocessor** that compresses injected content using LLM-native token compression. It exploits how transformer models process subword tokens — abbreviated forms like "auth" activate the same semantic representations as "authentication", saving tokens without losing meaning.

### Estimated Token Savings

| Level | Name | Savings per Injection | Per 100-Turn Session | Best For |
|-------|------|----------------------|---------------------|----------|
| 0 | `off` | 0% | 0 tokens | Debugging, transparency |
| 1 | `light` | ~5-10% | ~200-500 tokens | Minimal overhead, safe for all content |
| 2 | `standard` | ~10-20% | ~2,000-4,000 tokens | **Recommended** — good balance of compression and readability |
| 3 | `aggressive` | ~20-35% | ~4,000-8,000 tokens | Long sessions, large rule sets, cost optimization |

With `CONTEXT_COMPRESSION_SCOPE=all`, compression extends beyond context refresh to **all injected content**: session start banners, tool output, secrets warnings, circuit breaker messages, and more — multiplying savings across every hook event.

### Safety Guarantees

The preprocessor **never modifies** critical operational tokens:
- Negation words (`never`, `don't`, `not`, `without`)
- Action verbs (`push`, `delete`, `commit`, `deploy`)
- Code blocks, CLI commands, file paths
- Environment variable names, numbers, thresholds

### Quick Setup

```bash
# In ~/.agentihooks/.env
CONTEXT_REFRESH_COMPRESSION=standard    # compression level (default: standard)
CONTEXT_COMPRESSION_SCOPE=all           # compress all injections, not just refresh
```

Full architecture: [Context Preprocessor Docs](https://the-cloud-clock-work.github.io/agentihooks/docs/hooks/context-preprocessor/)

---

## Broadcast System — Real-Time Fleet Messaging

> **No other tool does this.** Send operator directives to every active Claude Code session simultaneously — like a PA system for your AI workforce.

Sessions auto-register when they start and deregister when they end. Every hook event polls `~/.agentihooks/broadcast.json` — no sockets, no server, no infrastructure required. Works on a laptop, NFS mount, or Redis-backed Kubernetes cluster identically.

### Manual broadcast

```bash
# Emergency stop — hits every agent on the next tool call
agentihooks broadcast -s critical "Production incident — read-only mode, do NOT deploy"

# Deploy freeze with custom TTL
agentihooks broadcast -s alert -t 8h "Deploy freeze until 6am"

# One-shot info (delivered once per session)
agentihooks broadcast -s info "SonarQube is down, skip CI validation"

# Manage active broadcasts
agentihooks broadcast --list       # show all active messages + TTLs
agentihooks broadcast --clear      # clear all
```

### AI-assisted emit — describe intent in plain English

```bash
# Haiku parses the severity and TTL from your natural language
agentihooks broadcast emit "production is on fire, all agents stop deploying immediately"
# → severity: critical, TTL: 30m, message: "production is on fire — stop deploying"

agentihooks broadcast emit "remind everyone that the API rate limits reset at midnight"
# → severity: info, TTL: 4h, message: "API rate limits reset at midnight"
```

`emit` is sandboxed: Claude Haiku can **only** run `agentihooks broadcast` commands — `Bash(agentihooks*)` is the sole permitted tool and all others are explicitly disallowed. It cannot read files, make network calls, or escape the broadcast surface.

### Severity levels

| Severity | Delivered | Default TTL | Use case |
|----------|-----------|-------------|----------|
| `critical` | Every user turn **and** every tool call | 30 min | Incidents, immediate stops |
| `alert` | Every user turn | 1 hour | Deploy freezes, degraded dependencies |
| `info` | Once per session | 4 hours | Reminders, FYI notices |

Full architecture: [Broadcast System Docs](https://the-cloud-clock-work.github.io/agentihooks/docs/hooks/broadcast/)

---

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

`scripts/sync_daemon.py` watches 27+ source files across all 3 layers (built-in, bundle, profile) and auto-propagates changes to every registered target within one poll cycle. The daemon is always restarted on `agentihooks init` to ensure it runs the latest code.

The daemon also performs automatic maintenance:
- **Orphan pruning** — removes MCP servers from `~/.claude.json` that are no longer defined in any source file
- **Hierarchy-aware blacklisting** — new MCP servers are disabled in all projects except those that whitelist them via `.agentihooks.json`
- **New project detection** — auto-blacklists MCPs for newly discovered project entries, respecting per-project whitelists

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
| `aws` | 3 | Profile listing, account discovery |
| `email` | 1 | SMTP send with text / HTML / markdown |
| `messaging` | 2 | SQS + webhook with state enrichment |
| `storage` | 1 | S3 upload |
| `database` | 2 | DynamoDB put, PostgreSQL execute |
| `compute` | 1 | Lambda invocation (sync/async) |
| `observability` | 2 | Session log diagnostics, container log tailing |
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
| `CONTEXT_REFRESH_ENABLED` | `true` | Re-inject rules and CLAUDE.md every N turns for attention decay mitigation |
| `CONTEXT_REFRESH_INTERVAL` | `20` | Re-inject rules every N user messages |
| `CONTEXT_REFRESH_CLAUDE_MD_INTERVAL` | `40` | Re-inject CLAUDE.md every N user messages (0 = disabled) |
| `CONTEXT_REFRESH_INCLUDE_PROJECT` | `true` | Also re-inject project-level `.claude/rules/` (not just global) |
| `CONTEXT_REFRESH_MAX_CHARS` | `8000` | Max chars per injection (rules or CLAUDE.md) — excess is truncated |
| `CONTEXT_REFRESH_RULES_DIR` | `~/.claude/rules` | Global rules directory to re-inject |
| `CONTEXT_REFRESH_COMPRESSION` | `standard` | Token compression: off/light/standard/aggressive |
| `CONTEXT_COMPRESSION_SCOPE` | `refresh` | Compression scope: refresh (default) or all (every injection) |
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
|-- mcp-tool-cache.json        # cached MCP tool counts (1h TTL, auto-refreshed)
+-- known-mcp-servers.json     # tracked server names for orphan detection
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
agentihooks init --local                 # shorthand for --repo . (current directory)
```

This reads `.agentihooks.json` from the repo root and generates:
- `.claude/settings.local.json` — permissions, env vars, MCP whitelist
- `.claude/CLAUDE.local.md` — profile system prompt (concatenated for chains)

Both files are gitignored automatically.

### `.agentihooks.json`

```json
{
  "profile": "coding",
  "enabledMcpServers": [
    "gateway-publish",
    "gateway-core",
    "gateway-pm"
  ]
}
```

| Field | Description |
|-------|-------------|
| `profile` | Profile name or comma-separated chain (e.g. `"coding,colt"`). Overrides the global profile for this project. |
| `enabledMcpServers` | MCP servers to whitelist for this project. All other servers are disabled by default. |
| `disabledMcpServers` | Additional servers to disable (project-scope `.mcp.json` connectors). |
| `permissions.deny` | Extra tool patterns to deny. |
| `permissions.ask` | Extra tool patterns requiring confirmation. |
| `env` | Additional env vars merged into `settings.local.json`. |
| `otel` | OpenTelemetry overrides for this project. |

### Profile override

The `profile` field controls which profile's settings are applied to this project:
- **Settings overrides** — env vars, permissions from the profile's `settings.overrides.json`
- **CLAUDE.local.md** — generated from the profile's `CLAUDE.md` (concatenated for chains)
- **MCP whitelist** — the profile's `enabledMcpServers` from `profile.yml` are unioned with the repo's whitelist

```bash
# Query the active profile for the current directory
agentihooks --query
# coding (local)       <-- reads .agentihooks.json
# colt (global)        <-- falls back to global if no local config
```

### Hierarchy-aware MCP blacklist

When parent and child projects both exist in `~/.claude.json`, the parent's disabled list automatically excludes servers that any child project whitelists. This prevents Claude Code's upward settings resolution from overriding child whitelists.

```
/agentihub                     → all MCPs disabled EXCEPT those child projects whitelist
/agentihub/agents/pub/package  → gateway-publish, gateway-core, gateway-pm enabled
```

The parent will NOT block `gateway-publish`, `gateway-core`, or `gateway-pm` — even though they aren't in its own whitelist.

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
