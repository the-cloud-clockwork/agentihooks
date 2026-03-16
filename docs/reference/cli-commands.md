---
title: CLI Commands
nav_order: 3
---

# CLI Commands
{: .no_toc }

The `agentihooks` CLI is installed globally via `uv tool install --editable .` as part of `agentihooks global`. All subcommands are idempotent.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## `agentihooks global`

Install hooks, skills, agents, and `CLAUDE.md` into `~/.claude`.

```bash
agentihooks global [--profile <name>] [--list-profiles] [--query]
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
agentihooks global

# Install with the coding profile
agentihooks global --profile coding

# Same, using the environment variable
AGENTIHOOKS_PROFILE=coding agentihooks global

# Auto-merge a gateway MCP file during install
AGENTIHOOKS_MCP_FILE=/shared/gateway-mcp.json agentihooks global

# List available profiles
agentihooks global --list-profiles

# Query active profile
agentihooks global --query
```

---

## `agentihooks project`

Write a rendered `.mcp.json` into a specific project directory.

```bash
agentihooks project <path> [--profile <name>]
```

This makes agentihooks MCP tools available in a single project without a global install. The `.mcp.json` is written to `<path>/.mcp.json`.

### Flags

| Flag | Description |
|------|-------------|
| `--profile <name>` | Profile whose MCP config to use (default: `default`, env: `AGENTIHOOKS_PROFILE`) |

### Example

```bash
agentihooks project ~/dev/my-project
agentihooks project ~/dev/my-project --profile coding
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

## `agentihooks --mcp`

Manage MCP servers at user scope (`~/.claude.json`), making them available in every project.

```bash
# Add MCP servers from a file
agentihooks --mcp /path/to/.mcp.json

# Remove a specific file's servers
agentihooks --mcp /path/to/.mcp.json --uninstall

# Interactive uninstall — pick from tracked files
agentihooks --mcp --uninstall
```

When adding, the file path is recorded in `~/.agentihooks/state.json` so `agentihooks global` can re-apply it automatically on future runs.

### Interactive uninstall

`agentihooks --mcp --uninstall` (no path) shows a numbered list of all tracked files with server counts and `[installed]` markers:

```
Tracked MCP files:

  1. /home/user/.agentitools/.anton-mcp.json
     14 server(s): anton, litellm, matrix, github, ...

Select file to uninstall [1-1] (or q to quit):
```

After removing, restart Claude Code for the change to take effect.

### Flags

| Flag | Description |
|------|-------------|
| `PATH` (optional) | Path to `.mcp.json` file. Omit with `--uninstall` for interactive selection. |
| `--uninstall` | Remove servers instead of adding them |

---

## `agentihooks --mcp-lib`

Browse a directory of `.mcp.json` files and interactively install one. The directory path is saved in `state.json` — omit it on future calls to reuse.

```bash
# First use — set the library directory
agentihooks --mcp-lib /path/to/mcp-library/

# Future calls — reuses the saved path
agentihooks --mcp-lib
```

### What it does

Scans the directory for any `.json` file containing a `mcpServers` key, shows a numbered list with server names and `[installed]` markers for already-tracked files, and installs the selected file via the standard `--mcp` flow.

```
MCP files in /home/user/.agentitools:

  1. .anton-mcp.json  [installed]
     14 server(s): anton, litellm, matrix, github, ...
  2. staging-mcp.json
     3 server(s): staging-api, staging-db, staging-cache

Select file to install [1-2] (or q to quit):
```

### State

The library path is saved as `mcpLibPath` in `~/.agentihooks/state.json` on first use. Change it any time by passing a new path.

---

## `agentihooks --sync`

Re-apply all MCP files previously registered via `--mcp`.

```bash
agentihooks --sync
```

Reads `~/.agentihooks/state.json` and merges each recorded `.mcp.json` file back into `~/.claude.json`. Called automatically by `agentihooks global` when `state.json` exists.

---

## `agentihooks --loadenv`

Installs an `agentienv` shell alias into `~/.bashrc` that sources `~/.agentihooks/.env` into the current shell on demand.

```bash
agentihooks --loadenv
```

### What it writes

A managed block in `~/.bashrc` (idempotent — safe to re-run):

```bash
# === agentihooks ===
alias agentienv='set -a && . /home/user/.agentihooks/.env && set +a'
# === end-agentihooks ===
```

### Usage

```bash
# Install the alias (one time)
agentihooks --loadenv

# Reload your shell
source ~/.bashrc

# Load all vars into the current shell whenever needed
agentienv
```

After `agentienv`, all vars from `.env` are in the current shell. Start Claude Code from that shell and all `${VAR}` placeholders in MCP configs will resolve.

### Why this exists

Claude Code expands `${VAR}` in MCP server configs from its own process environment at startup. Variables defined only in hook subprocesses arrive too late. `agentienv` loads them into the launching shell so `claude` inherits them.

### Custom path

Pass a different env file path to point the alias elsewhere:

```bash
agentihooks --loadenv /path/to/other.env
```

### Managed block

The `# === agentihooks === / # === end-agentihooks ===` markers make the block idempotent and upgradeable — re-running `--loadenv` replaces the block contents rather than appending. Keep your own aliases **outside** the markers.

### Auto-installing requirements

After writing the alias, `--loadenv` scans `~/.agentihooks/` and the saved `mcpLibPath` for `requirements.txt` files and offers to install each:

```
Found /home/user/.agentitools/requirements.txt — install with uv? [y/N]
```

- Uses `uv pip install --python <venv> -r requirements.txt`
- Detects venv via `$VIRTUAL_ENV` or `.venv` in the current directory
- **Refuses to install into system Python** — activating a venv first is required
- Skipped entirely if `uv` is not found on `$PATH`

---

## Standalone Python execution

The hook and MCP server modules can be run directly with Python:

```bash
# Run the MCP tool server (all 45 tools)
python -m hooks.mcp

# Run with specific categories
MCP_CATEGORIES=github,utilities python -m hooks.mcp

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
