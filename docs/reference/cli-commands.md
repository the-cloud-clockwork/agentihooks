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

Install hooks, skills, agents, and `CLAUDE.md` into `~/.claude`.

```bash
agentihooks init [--profile <name>] [--list-profiles] [--query]
```

### What it does

1. Reads `profiles/_base/settings.base.json`
2. Substitutes `/app` → real repo path in all commands
3. Preserves personal keys (`model`, `autoUpdatesChannel`, `skipDangerousModePermissionPrompt`) from any pre-existing unmanaged settings
4. Writes `~/.claude/settings.json` with hook wiring and tool permissions
5. Symlinks skills, agents, and commands from `.claude/` into `~/.claude/`
6. Symlinks `~/.claude/CLAUDE.md` → chosen profile's `CLAUDE.md`
7. Merges profile `.mcp.json` into `~/.claude.json` (user-scope MCP servers)
8. If `~/.agentihooks/state.json` exists, re-syncs any custom MCP files via `--sync`
9. If `AGENTIHOOKS_MCP_FILE` is set and the file exists, merges it into `~/.claude.json` and records the path in `state.json`

### Flags

| Flag | Description |
|------|-------------|
| `--profile <name>` | Profile to install (default: `default`, env: `AGENTIHOOKS_PROFILE`) |
| `--list-profiles` | Print all available profiles and exit |
| `--query` | Print the currently active profile name and exit |

### Environment variables

| Variable | Description |
|----------|-------------|
| `AGENTIHOOKS_PROFILE` | Default profile when `--profile` is not passed (default: `default`) |
| `AGENTIHOOKS_MCP_FILE` | Path to an MCP JSON file to auto-merge into `~/.claude.json` during install |
| `CLAUDE_CODE_HOME_DIR` | Home-directory root override — `.claude` is appended automatically (default: `$HOME`) |
| `AGENTIHOOKS_CLAUDE_HOME` | Legacy: direct path to the `.claude` directory (default: `~/.claude`) |

### Examples

```bash
# Install with default profile
agentihooks init

# Install with the coding profile
agentihooks init --profile coding

# Same, using the environment variable
AGENTIHOOKS_PROFILE=coding agentihooks init

# Auto-merge a gateway MCP file during install
AGENTIHOOKS_MCP_FILE=/shared/gateway-mcp.json agentihooks init

# List available profiles
agentihooks init --list-profiles

# Query active profile
agentihooks init --query
```

---

## `agentihooks init --repo`

Write a rendered `.mcp.json` into a specific project directory.

```bash
agentihooks init --repo <path> [--profile <name>]
```

This makes agentihooks MCP tools available in a single project without a global install. The `.mcp.json` is written to `<path>/.mcp.json`.

### Flags

| Flag | Description |
|------|-------------|
| `--profile <name>` | Profile whose MCP config to use (default: `default`, env: `AGENTIHOOKS_PROFILE`) |

### Example

```bash
agentihooks init --repo ~/dev/my-project
agentihooks init --repo ~/dev/my-project --profile coding
```

---

## `agentihooks uninstall`

Remove everything agentihooks installed from the system.

```bash
agentihooks uninstall [--yes]
```

### What gets removed

- `~/.claude/settings.json` — if managed by agentihooks (detected via `_managedBy` marker)
- Skills, agents, and command symlinks in `~/.claude/` — if they target the agentihooks repo
- `~/.claude/CLAUDE.md` — if it points into `profiles/`
- MCP servers in `~/.claude.json` — from profile `.mcp.json` files and `state.json`
- `agentihooks` CLI — via `uv tool uninstall agentihooks`

### What is NOT removed

`~/.agentihooks/` (user data: logs, memory, state) is left in place. To fully reset:

```bash
rm -rf ~/.agentihooks
```

### Flags

| Flag | Description |
|------|-------------|
| `--yes` | Skip confirmation prompt (for scripting) |

---

## `agentihooks ignore`

Create a `.claudeignore` in the current working directory (or a given path). Claude Code uses `.claudeignore` to exclude files from reading and indexing — keeping credentials, build artefacts, and binaries out of the context window.

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

### Idempotency

Without `--force`, the command skips if `.claudeignore` already exists:

```
  [--] /home/user/project/.claudeignore already exists — use --force to overwrite
```

### After creating

Edit the file to add project-specific patterns. Use the same syntax as `.gitignore`:

```gitignore
# Project-specific additions
fixtures/large-dataset.json
docs/generated/
```

---

## `agentihooks mcp`

Manage MCP server files at user scope (`~/.claude.json`). Drop `.json` files with a `mcpServers` key into `~/.agentihooks/`, then use the interactive commands to install or remove them.

```bash
agentihooks mcp                     # list available MCP files (default action)
agentihooks mcp install             # two-stage: pick file → pick servers
agentihooks mcp uninstall           # two-stage: pick file → pick servers to remove
agentihooks mcp add <path>          # install a file directly by path
agentihooks mcp sync                # re-apply all installed MCP files
agentihooks mcp list --dir <path>   # scan a different directory
```

### Interactive two-stage flow (`install` and `uninstall`)

Both `install` and `uninstall` use the same two-stage interactive flow:

**Stage 1 — pick a file:**

If only one file is present, it is auto-displayed without a prompt. Otherwise a numbered list is shown:

```
MCP files in /home/user/.agentihooks:

  1. anton-mcp.json  [installed]
     • anton
     • litellm
     • matrix
     • github
  2. staging-mcp.json
     • staging-api
     • staging-db
     • staging-cache

Enter file number (1-2, or q to quit):
```

**Stage 2 — pick which servers to install/uninstall:**

```
Servers in anton-mcp.json:

  0. All (4 servers)
  1. anton
  2. litellm
  3. matrix
  4. github

Select (0=all, 1-4, or comma list):
```

Enter `0` for all, a single number, or a comma-separated list (e.g. `1,3`).

For `uninstall`: the file is removed from `state.json` only if **all** of its servers were uninstalled.

### How it works

1. Scans `~/.agentihooks/` (or `--dir`) for any `.json` file containing a `mcpServers` key
2. Shows a numbered list with server names as bullet points (`•`) and `[installed]` markers
3. On install, merges the selected servers into `~/.claude.json` and tracks the file in `state.json`
4. On uninstall, removes the selected servers from `~/.claude.json`; removes the file from tracking only if all servers were removed

### Actions

| Action | Description |
|--------|-------------|
| `list` (default) | Show all MCP files in the scan directory with install status and bullet-point server names |
| `install` | Two-stage: pick a file, then pick which servers to install |
| `uninstall` | Two-stage: pick an installed file, then pick which servers to remove |
| `add <path>` | Install a specific file directly by path (all servers, no prompting) |
| `sync` | Re-apply all tracked MCP files from `state.json` (called automatically by `agentihooks init`) |

### Flags

| Flag | Description |
|------|-------------|
| `--dir <path>` | Override the scan directory (default: `~/.agentihooks/`) |

### Workflow

```bash
# Drop a new MCP file into the library
cp my-servers.json ~/.agentihooks/

# Browse and install (two-stage: pick file, then pick servers)
agentihooks mcp install

# After changing MCP files, re-sync
agentihooks mcp sync

# Restart Claude Code to pick up changes
```

---

## `agentihooks daemon`

Manage the sync daemon that watches all source files (profiles, settings, connectors, bundles, MCPs, `.env`) and auto-propagates changes to all registered downstream consumers.

```bash
agentihooks daemon                 # start background daemon (60s poll)
agentihooks daemon status          # show targets, watched files, last scan
agentihooks daemon logs            # tail -f daemon log
agentihooks daemon stop            # kill daemon
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `start` *(default)* | Start the background daemon. Auto-detaches, writes PID to `~/.agentihooks/sync-daemon.pid`, logs to `~/.agentihooks/logs/sync-daemon.log`. |
| `stop` | Kill the running daemon using the PID from `~/.agentihooks/sync-daemon.pid`. |
| `status` | Show daemon PID, registered targets (global + projects), watched file count, and last scan timestamp. Flags `[PATH MISSING]` for project paths that no longer exist. |
| `logs` | Runs `tail -f` on `~/.agentihooks/logs/sync-daemon.log`. |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--poll N` | `60` | Poll interval in seconds. Also configurable via `AGENTIHOOKS_SYNC_POLL_SEC` env var. |
| `--foreground` | — | Run in foreground instead of daemonizing. Useful for debugging. |

### Target registration

Targets are registered automatically:
- `agentihooks init` registers `~/.claude/` as the global target with the chosen profile.
- `agentihooks init --repo <path>` registers the project path with the chosen profile.

Registered targets are stored in `~/.agentihooks/state.json` under the `targets` key:

```json
{
  "targets": {
    "global": { "path": "/home/user/.claude", "profile": "default" },
    "projects": {
      "/home/user/dev/my-project": { "profile": "coding" }
    }
  }
}
```

### Propagation rules

| Source change | Affected targets |
|---|---|
| `settings.base.json` | Global + all projects + MCP sync |
| Profile files (`profile.yml`, `settings.overrides.json`, `CLAUDE.md`) | Targets using that profile |
| Connector files (`connector.yml`, `permissions.json`, `env.json`) | Global + all projects |
| MCP files (tracked in `state.json`) | MCP sync only (`~/.claude.json`) |
| `.env` files in `~/.agentihooks/` | Global + all projects |
| Bundle directory contents | Global + all projects |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_SYNC_POLL_SEC` | `60` | Daemon poll interval in seconds. |

---

## `agentihooks quota`

Manage the background quota watcher daemon that scrapes claude.ai/settings/usage and writes `~/.agentihooks/claude_usage.json` for the statusline.

```bash
agentihooks quota                  # start background daemon
agentihooks quota auth             # open browser, paste sessionKey, import cookie, start daemon
agentihooks quota import-cookies   # paste sessionKey without opening browser
agentihooks quota status           # print last known quota JSON
agentihooks quota logs             # tail -f daemon log
agentihooks quota stop             # kill daemon
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| *(none)* | Start the background daemon. Auto-detaches, writes PID to `~/.agentihooks/quota-watcher.pid`, logs to `~/.agentihooks/logs/quota-watcher.log`. |
| `auth` | Opens YOUR real system browser (Chrome on Windows/WSL via `cmd.exe /c start`, Safari/Chrome on Mac via `open`) to claude.ai. Prompts for the `sessionKey` cookie (F12 → Application → Cookies → claude.ai → sessionKey). Imports the cookie into Playwright storage state at `~/.agentihooks/claude_auth_state.json`, then starts the daemon. |
| `import-cookies` | Same as `auth` but skips opening the browser — paste the `sessionKey` value directly. |
| `status` | Print the last known quota JSON from `~/.agentihooks/claude_usage.json`. |
| `logs` | Runs `tail -f` on `~/.agentihooks/logs/quota-watcher.log`. |
| `stop` | Kill the running daemon using the PID from `~/.agentihooks/quota-watcher.pid`. |

### Auth flow

No Chromium/Playwright browser is used for authentication. The `auth` subcommand opens the user's real browser so they can access claude.ai with their existing session. The user copies the `sessionKey` cookie value from Chrome DevTools and pastes it at the prompt. The cookie is saved as a Playwright `storage_state` JSON file at `~/.agentihooks/claude_auth_state.json`. Headless Chromium is only used by the background daemon for scraping.

### Statusline output

When enabled (set `CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json` in `~/.agentihooks/.env`), the statusline displays quota information on line 3:

```
session:53% [1h] | all:35% resets fri 10:00 am | sonnet:5% resets mon 12:00 am | extra: €40/99 (40%) resets apr 1
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_USAGE_FILE` | — | Must be set to enable. Path to the quota JSON written by the daemon. |
| `CLAUDE_USAGE_STALE_SEC` | `300` | Data older than this shows "stale" on the statusline. |
| `CLAUDE_USAGE_POLL_SEC` | `60` | Daemon poll interval in seconds. |

### Prerequisites

```bash
~/.agentihooks/.venv/bin/python -m playwright install chromium
```

---

## `agentihooks init`

Installs an `agentienv` **shell function** (not an alias) into `~/.bashrc` that sources all `.env` files from `~/.agentihooks/` into the current shell on demand. The function is also **auto-called** at the end of the managed block so vars load in every new shell automatically.

```bash
agentihooks init
```

### What it writes

A managed block in `~/.bashrc` (idempotent — safe to re-run):

```bash
# === agentihooks ===
agentienv() {
  if [ ! -f "~/.agentihooks/.env" ]; then
    echo "[agentienv] no .env found at ~/.agentihooks/.env — skipping"
    return 0
  fi
  set -a
  . "~/.agentihooks/.env"
  _aih_count=1
  for f in "~/.agentihooks/"*.env; do
    [ -f "$f" ] && [ "$f" != "~/.agentihooks/.env" ] && {
      . "$f"
      _aih_count=$((_aih_count + 1))
    }
  done
  set +a
  echo "[agentienv] loaded $_aih_count env file(s) from ~/.agentihooks"
}
agentienv
# === end-agentihooks ===
```

This loads `~/.agentihooks/.env` first, then any additional `*.env` files (e.g., companion env files for MCP configs) in alphabetical order. The trailing `agentienv` call means vars are loaded automatically in every new shell — no manual invocation needed unless you add new env files mid-session.

### Usage

```bash
# Install the alias (one time)
agentihooks init

# Reload your shell
source ~/.bashrc

# Load all vars into the current shell whenever needed
agentienv
```

After `agentienv`, all vars from `.env` and any companion `*.env` files are in the current shell. Start Claude Code from that shell and all `${VAR}` placeholders in MCP configs will resolve.

### Why this exists

Claude Code expands `${VAR}` in MCP server configs from its own process environment at startup. Variables defined only in hook subprocesses arrive too late. `agentienv` loads them into the launching shell so `claude` inherits them.

### Custom path

Pass a different env file path to point the alias elsewhere:

```bash
agentihooks init /path/to/other.env
```

### Managed block

The `# === agentihooks === / # === end-agentihooks ===` markers make the block idempotent and upgradeable — re-running `init` replaces the block contents rather than appending. Keep your own aliases **outside** the markers.

### Auto-installing requirements

After writing the alias, `init` scans `~/.agentihooks/` and the saved `mcpLibPath` for `requirements.txt` files and offers to install each:

```
Found /home/user/.agentitools/requirements.txt — install with uv? [y/N]
```

- Uses `uv pip install --python <venv> -r requirements.txt`
- Detects venv via `$VIRTUAL_ENV` or `.venv` in the current directory
- **Refuses to install into system Python** — activating a venv first is required
- Skipped entirely if `uv` is not found on `$PATH`

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

### Example

```
CLAUDE.md Lint Report: /home/user/.claude/CLAUDE.md
Total: 11,235 chars ≈ 2,808 tokens

Section                                    Tokens       Type
──────────────────────────────────────── ──────── ──────────
*Commands                                     224   workflow
 Architecture                                   4     always
...

Extraction candidates (8 sections, ~2,164 tokens):
  * "Commands" (224 tokens, lines 5-35)
  ...
```

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

The section is removed from the source CLAUDE.md and written as a SKILL.md in the new skill directory.

---

## `agentihooks mcp report`

Analyze MCP server configurations and report estimated token overhead.

```bash
agentihooks mcp report [--project <path>]
```

### What it shows

- Per-server tool count (introspected for hooks-utils, heuristic for external servers)
- Estimated schema token cost per server
- Total tool and token overhead

### Flags

| Flag | Description |
|------|-------------|
| `--project` | Project path to include in the scan (default: CWD). |

### Example

```
MCP Surface Area Report
Total: 9 servers, ~112 tools, ~16,800 schema tokens

Server                         Source   Tools   ~Tokens
hooks-utils                      user      32     4,800
github                           user      40     6,000
...
```

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
