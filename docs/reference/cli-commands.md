---
title: CLI Commands
nav_order: 3
---

# CLI Commands
{: .no_toc }

The `agentihooks` CLI is installed globally via `uv tool install --editable .` as part of `agentihooks init`. All subcommands are idempotent.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## `agentihooks init`

The single entry point for installing agentihooks. Handles global setup, bundle linking, and per-repo configuration.

```bash
agentihooks init [--bundle <path>] [--profile <name>] [--repo <path>]
```

### What it does

1. Links bundle directory (if `--bundle` is provided)
2. Merges settings: `_base/settings.base.json` -> profile `.claude/settings.overrides.json` -> OTEL
3. Substitutes `/app` -> real repo path and `__PYTHON__` -> venv Python in all commands
4. Preserves personal keys (`model`, `autoUpdatesChannel`, `skipDangerousModePermissionPrompt`) from any pre-existing unmanaged settings
5. Writes `~/.claude/settings.json` with hook wiring and tool permissions
6. Symlinks skills, agents, commands, and rules via 3-layer merge (agentihooks built-in -> bundle global -> profile-specific)
7. Symlinks `~/.claude/CLAUDE.md` -> chosen profile's `CLAUDE.md` (at profile root)
8. Installs MCPs (hooks-utils + bundle `.claude/.mcp.json` + profile `.claude/.mcp.json`)
9. Applies MCP blacklist across all projects (`disabledMcpServers`)
10. Installs the `agentihooks` CLI globally via `uv tool install --editable .`
11. Auto-starts quota and sync daemons
12. Writes managed bashrc block (`agentienv` function + `agenti` alias)

### Flags

| Flag | Description |
|------|-------------|
| `--bundle <path>` | Path to bundle directory. First-time: links the bundle and runs global install. |
| `--profile <name>` | Profile to install (default: `default`, env: `AGENTIHOOKS_PROFILE`) |
| `--repo <path>` | Target repo directory for per-repo configuration |

### Environment variables

| Variable | Description |
|----------|-------------|
| `AGENTIHOOKS_PROFILE` | Default profile when `--profile` is not passed (default: `default`) |
| `AGENTIHOOKS_MCP_FILE` | Path to an MCP JSON file to auto-merge into `~/.claude.json` during install |
| `CLAUDE_CODE_HOME_DIR` | Home-directory root override -- `.claude` is appended automatically (default: `$HOME`) |
| `AGENTIHOOKS_CLAUDE_HOME` | Legacy: direct path to the `.claude` directory (default: `~/.claude`) |

### Examples

```bash
# First-time install with bundle
agentihooks init --bundle ~/dev/my-tools --profile coding

# Re-run global install (uses linked bundle)
agentihooks init

# Install with a different profile
agentihooks init --profile admin

# Same, using the environment variable
AGENTIHOOKS_PROFILE=coding agentihooks init

# Per-repo configuration
agentihooks init --repo ~/dev/my-project

# Auto-merge a gateway MCP file during install
AGENTIHOOKS_MCP_FILE=/shared/gateway-mcp.json agentihooks init
```

---

## `agentihooks uninstall`

Remove everything agentihooks installed from the system.

```bash
agentihooks uninstall [--yes]
```

### What gets removed

- `~/.claude/settings.json` -- if managed by agentihooks (detected via `_managedBy` marker)
- Skills, agents, commands, and rules symlinks in `~/.claude/` -- if they target the agentihooks repo
- `~/.claude/CLAUDE.md` -- if it points into `profiles/`
- MCP servers in `~/.claude.json` -- from profile `.mcp.json` files and `state.json`
- Running daemons -- both quota watcher and sync daemon are stopped
- Bashrc block -- the `agentienv` function and `agenti` alias are removed from `~/.bashrc`
- `agentihooks` CLI -- via `uv tool uninstall agentihooks`

### What is NOT removed

`~/.agentihooks/` (user data: logs, memory, state.json) is left in place. To fully reset:

```bash
rm -rf ~/.agentihooks
```

### Flags

| Flag | Description |
|------|-------------|
| `--yes` | Skip confirmation prompt (for scripting) |

---

## `agentihooks claude`

Launch Claude Code with flags derived from the active profile's `profile.yml`.

```bash
agentihooks claude [extra-args...]
```

**Alias:** `agenti` (installed by `agentihooks init` in the bashrc block)

### How it works

Reads the `claude:` section from the active profile's `profile.yml` and maps fields to Claude Code CLI flags:

| profile.yml field | CLI flag |
|-------------------|----------|
| `claude.model` | `--model <value>` |
| `claude.max_turns` | `--max-turns <value>` |
| `claude.permission_mode: bypassPermissions` | `--dangerously-skip-permissions` |

Any extra arguments are passed through to Claude Code.

### Examples

```bash
# Launch with profile settings
agentihooks claude

# Use the alias
agenti

# Pass extra args to claude
agenti --verbose
```

---

## `agentihooks quota`

Manage the background quota watcher daemon that scrapes claude.ai/settings/usage. Supports multiple accounts.

```bash
agentihooks quota [action] [account-name]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| *(none)* / `watch` | Start the background daemon. Auto-detaches, writes PID to `~/.agentihooks/quota-watcher.pid`, logs to `~/.agentihooks/logs/quota-watcher.log`. |
| `auth [name]` | Opens YOUR real system browser to claude.ai. Prompts for the `sessionKey` cookie (F12 -> Application -> Cookies -> claude.ai -> sessionKey). Saves credentials to `~/.agentihooks/quota-accounts/<name>.json`, then starts the daemon. |
| `import-cookies` | Same as `auth` but skips opening the browser -- paste the `sessionKey` value directly. |
| `list` | Show all configured quota accounts. |
| `switch <name>` | Switch the active quota account. |
| `restart` | Restart the daemon with the current active account. |
| `remove <name>` | Remove a quota account. |
| `status` | Print the last known quota JSON from `~/.agentihooks/claude_usage.json`. |
| `logs` | Runs `tail -f` on `~/.agentihooks/logs/quota-watcher.log`. |
| `stop` | Kill the running daemon. |
| `dump-html` | Dump raw usage page HTML to `~/.agentihooks/usage_debug.html` for scraper debugging. |

### Multi-account support

Account credentials are stored at `~/.agentihooks/quota-accounts/<name>.json`. You can authenticate multiple accounts and switch between them:

```bash
# Authenticate accounts
agentihooks quota auth work
agentihooks quota auth personal

# List all accounts
agentihooks quota list

# Switch active account
agentihooks quota switch personal

# Restart daemon with current account
agentihooks quota restart

# Remove an account
agentihooks quota remove personal
```

### Auth flow

No Chromium/Playwright browser is used for authentication. The `auth` subcommand opens the user's real browser so they can access claude.ai with their existing session. The user copies the `sessionKey` cookie value from Chrome DevTools and pastes it at the prompt. Headless Chromium is only used by the background daemon for scraping.

### Statusline output

When enabled (set `CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json` in `~/.agentihooks/.env`), the statusline displays quota information on line 3:

```
session:53% [1h] | all:35% resets fri 10:00 am | sonnet:5% resets mon 12:00 am | extra: $40/99 (40%) resets apr 1
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--poll N` | `60` | Daemon poll interval in seconds. |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_USAGE_FILE` | -- | Must be set to enable. Path to the quota JSON written by the daemon. |
| `CLAUDE_USAGE_STALE_SEC` | `300` | Data older than this shows "stale" on the statusline. |
| `CLAUDE_USAGE_POLL_SEC` | `60` | Daemon poll interval in seconds. |

### Prerequisites

```bash
~/.agentihooks/.venv/bin/python -m playwright install chromium
```

---

## `agentihooks daemon`

Manage the sync daemon that watches asset directories (skills, agents, commands, rules, MCP servers, `.env` files) and auto-propagates changes to all registered downstream consumers.

```bash
agentihooks daemon [action]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `start` *(default)* | Start the background daemon. Auto-detaches, writes PID to `~/.agentihooks/sync-daemon.pid`, logs to `~/.agentihooks/logs/sync-daemon.log`. |
| `stop` | Kill the running daemon using the PID file. |
| `status` | Show daemon PID, registered targets, watched file count, and last scan timestamp. Flags `[PATH MISSING]` for project paths that no longer exist. |
| `logs` | Runs `tail -f` on `~/.agentihooks/logs/sync-daemon.log`. |

### How it works

The sync daemon uses manifest hashing to detect changes. On each poll cycle:

1. Hashes every source file (profiles, settings, bundles, MCP files, `.env` files) using SHA-256
2. Compares against previous hashes
3. If changes are detected, re-runs the install pipeline for affected targets
4. Propagation is **additive only** -- new skills/agents/commands/rules are symlinked automatically

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--poll N` | `60` | Poll interval in seconds. Also configurable via `AGENTIHOOKS_SYNC_POLL_SEC` env var. |
| `--foreground` | -- | Run in foreground instead of daemonizing. Useful for debugging. |

### Target registration

Targets are registered automatically:
- `agentihooks init` registers `~/.claude/` as the global target with the chosen profile.
- `agentihooks init --repo <path>` registers the project path with the chosen profile.

Registered targets are stored in `~/.agentihooks/state.json` under the `targets` key.

### Propagation rules

| Source change | Affected targets |
|---|---|
| `settings.base.json` | Global + all projects + MCP sync |
| Profile files (`profile.yml`, `settings.overrides.json`, `CLAUDE.md`) | Targets using that profile |
| MCP files | MCP sync only (`~/.claude.json`) |
| `.env` files in `~/.agentihooks/` | Global + all projects |
| Bundle directory contents | Global + all projects |
| Skills, agents, commands, rules | Re-symlinked via 3-layer merge |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_SYNC_POLL_SEC` | `60` | Daemon poll interval in seconds. |

---

## `agentihooks ignore`

Create a `.claudeignore` in the current working directory (or a given path). Claude Code uses `.claudeignore` to exclude files from reading and indexing -- keeping credentials, build artefacts, and binaries out of the context window.

```bash
agentihooks ignore [path] [--force]
```

### What it creates

A `.claudeignore` covering:

| Section | Examples |
|---------|---------|
| Credentials & secrets | `.env`, `.env.*`, `*.pem`, `*.key`, `secrets/` |
| Build artefacts | `__pycache__/`, `dist/`, `node_modules/`, `target/`, `*.egg-info/` |
| Runtime data | `*.log`, `*.sqlite`, `*.db`, `*.lock` |
| Test output | `.coverage`, `htmlcov/`, `junit*.xml` |
| IDE / OS noise | `.idea/`, `.vscode/`, `.DS_Store`, `Thumbs.db` |
| Large binaries / media | archives, images, video, fonts |
| Virtual environments | `.venv/`, `venv/`, `env/` |
| IaC state | `.terraform/`, `*.tfstate`, `.terraform.lock.hcl` |

`.env.example` is explicitly un-ignored (`!.env.example`) so the template remains visible.

### Flags

| Flag | Description |
|------|-------------|
| `path` | Target directory (default: current directory) |
| `--force` | Overwrite an existing `.claudeignore` |

### Examples

```bash
# Create in current directory
agentihooks ignore

# Create in a specific project
agentihooks ignore ~/dev/my-project

# Overwrite an existing file with a fresh template
agentihooks ignore --force
```

---

## `agentihooks --list-profiles`

Print all available profiles and exit. Shows profiles from both the agentihooks repo and any linked bundle.

```bash
agentihooks --list-profiles
```

---

## `agentihooks --query`

Print the currently active profile name and exit.

```bash
agentihooks --query
```

---

## `agentihooks status`

Show full system health, MCP fleet inventory with real tool counts, cost guardrails, and quota.

```bash
agentihooks status
```

### What it checks

| Check | What it does |
|-------|-------------|
| **Profile** | Reads `state.json` for active profile and bundle path |
| **Hooks** | Parses `~/.claude/settings.json`, counts hook event entries (expect 10/10) |
| **Python** | Extracts the Python binary from hook commands and verifies it runs |
| **Daemons** | Checks PID files for sync and quota daemons, verifies processes are alive |
| **Redis** | Pings Redis, categorizes all `agenticore:*` keys by type |
| **OTEL** | Checks if OpenTelemetry hook telemetry is enabled |
| **Guardrails** | Lists all 6 cost guardrails with descriptions and enabled/disabled state |
| **MCP** | Reads `~/.claude.json` for all servers, resolves `${ENV_VAR}` auth, queries each HTTP server via MCP protocol for real tool counts, checks per-project blacklists, shows fleet total vs active in current project |
| **Quota** | Loads quota data, shows session/weekly/spend with peak/off-peak indicator |

### MCP fleet introspection

The status checker connects to every HTTP MCP server (even disabled ones) to get real tool counts. Auth tokens are resolved from `${ENV_VAR}` references in `~/.claude.json` headers using env vars loaded by `agentienv`. Results are cached at `~/.agentihooks/mcp-tool-cache.json` with a 1-hour TTL.

Per-project blacklists are read from the `projects` block in `~/.claude.json` (the blacklist-all-by-default mechanism). The output shows fleet total (all servers) vs active tools (enabled in current project context).

### In-session skill

The `/agentihooks` skill (delivered via the bundle at `.claude/skills/agentihooks/`) runs the same checker inside a Claude Code session with `--session $CLAUDE_SESSION_ID --json`, adding live session metrics: context fill %, burn rate, per-tool consumption from the context audit, and warning levels.

---

## `agentihooks lint-claude`

Analyze a CLAUDE.md file for token cost and suggest sections to extract into on-demand skills.

```bash
agentihooks lint-claude [path]
```

Defaults to `~/.claude/CLAUDE.md` if no path is given.

### Output

- Total character and token estimate
- Per-section breakdown with classification (always-needed vs workflow-specific)
- Extraction candidates with token savings estimate

---

## `agentihooks extract-skill`

Extract a section from CLAUDE.md into a standalone skill directory.

```bash
agentihooks extract-skill "<Section Heading>" --name <skill-name> [--source <path>] [--output-dir <path>]
```

### Flags

| Flag | Description |
|------|-------------|
| `--name` | Required. Name for the skill directory. |
| `--source` | Path to CLAUDE.md (default: `~/.claude/CLAUDE.md`). |
| `--output-dir` | Output directory (default: source's `.claude/commands/`). |

---

## Standalone Python execution

The hook and MCP server modules can be run directly with Python:

```bash
# Run the MCP tool server (all 26 tools)
python -m hooks.mcp

# Run with specific categories
MCP_CATEGORIES=aws,utilities python -m hooks.mcp

# Process a hook event manually
echo '{"hook_event_name":"SessionStart","session_id":"test-123"}' | python -m hooks

# Pipe a PreToolUse event
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' | python -m hooks
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Error (installation failed, missing config, etc.) |
| `2` | Block (used by hook handlers to cancel tool execution) |
