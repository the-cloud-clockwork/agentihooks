---
title: Portability & Reusability
nav_order: 4
parent: Getting Started
---

# Portability & Reusability
{: .no_toc }

AgentiHooks is designed to travel with you. One data directory, one env file, and an idempotent install command let you reproduce a complete Claude Code environment on any machine — or share a setup across a team.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The `~/.agentihooks/` data directory

Everything user-specific lives in a single directory:

```
~/.agentihooks/
├── .env          # Main credentials (always loaded first)
├── *.env         # Companion env files (auto-sourced alphabetically after .env)
├── *.json        # Drop MCP server files here → agentihooks mcp install
├── state.json    # Tracks installed MCP files and other state
├── logs/         # Hook + MCP log files
└── memory/       # Per-project agent memory files
```

`agentihooks uninstall` never touches this directory — your credentials and memory survive reinstalls.

To fully reset: `rm -rf ~/.agentihooks`

---

## Environment file (`~/.agentihooks/.env`)

All integration keys live in one place:

```bash
# MCP server credentials
MCP_ATLASSIAN_PROXY_API_KEY=...
MCP_SONAR_PROXY_API_KEY=...
MCP_AGENTIBRIDGE_API_KEY=...

# Service endpoints
LITELLM_URL=http://10.10.30.130:4000
GRAFANA_URL=http://10.10.30.130:3000

# GitHub
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...
```

The file is seeded from `.env.example` on first `agentihooks global` and is **never overwritten** on subsequent runs.

**To move to a new machine:** copy `~/.agentihooks/.env` alongside the repo clone.

---

## Loading env vars into your shell (`--loadenv`)

Claude Code expands `${VAR}` in MCP configs from its own process environment at startup. `--loadenv` installs a **shell function** (not an alias) that sources `.env` into any shell on demand — and also auto-calls it so vars load in every new shell automatically.

```bash
# Install the function (one time — writes a managed block to ~/.bashrc)
agentihooks --loadenv

# Reload your shell
source ~/.bashrc

# Vars are already loaded automatically. Call agentienv manually only
# if you add new env files mid-session:
agentienv
```

Then launch `claude` from that shell — all `${VAR}` placeholders in your MCP configs resolve correctly.

The function written to `~/.bashrc` defines `agentienv()` which:
1. Sources `~/.agentihooks/.env`
2. Sources all `*.env` files alphabetically from the same directory
3. Reports how many files were loaded

The block ends with a bare `agentienv` call so the vars are loaded automatically in every new shell.

The block is **idempotent** — re-running `--loadenv` updates the block in place rather than appending. Keep your own aliases outside the markers.

### Auto-installing requirements

After writing the alias, `--loadenv` scans `~/.agentihooks/` and the saved `mcpLibPath` for any `requirements.txt` and offers to install each one:

```
Found /home/user/.agentitools/requirements.txt — install with uv? [y/N]
```

Requirements are installed with `uv pip install` into the active virtual environment. If no venv is active and no `.venv` exists in the current directory, installation is refused to avoid polluting system Python:

```
[!!] No virtual environment found.
     Create and activate one first:
       python3 -m venv .venv && source .venv/bin/activate
     Then re-run: agentihooks --loadenv
```

---

## Managing MCP server files (`agentihooks mcp`)

Drop `.json` files with a `mcpServers` key into `~/.agentihooks/`, then use the interactive MCP manager to install or remove them.

```bash
# List available MCP files
agentihooks mcp

# Interactive install — pick from the list
agentihooks mcp install

# Interactive uninstall — pick from installed files
agentihooks mcp uninstall

# Install a specific file directly by path
agentihooks mcp add ~/Downloads/my-servers.json

# Re-apply all installed files after changes
agentihooks mcp sync
```

Output:

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

After picking a file, a second prompt lets you choose which servers to install (`0` = all, or specific numbers/comma list).

`[installed]` marks files already tracked in `state.json`. Installed servers are merged into `~/.claude.json` and re-applied automatically on `agentihooks global`.

For `uninstall`, the file is removed from tracking only if **all** its servers were uninstalled.

### Companion `.env` files

Drop a `.env` file alongside your MCP JSON to provide the env vars it needs:

```
~/.agentihooks/
├── anton-mcp.json     # MCP server definitions (${ANTON_HOST}, etc.)
├── anton-mcp.env      # ANTON_HOST=10.10.30.130, ANTON_API_KEY=...
├── staging.json
└── staging.env
```

Companion env files are auto-sourced by:
- **Hook runtime** — `hooks/config.py` loads `~/.agentihooks/.env` first, then all `*.env` files alphabetically
- **`agentienv` shell function** — sources `.env` then all `*.env` files so `${VAR}` placeholders in MCP configs resolve at Claude Code startup

The `mcp list` output shows detected companion env files:

```
  1. anton-mcp.json  [installed]
     • anton
     • litellm
     • matrix
     env: anton-mcp.env
```

To scan a different directory: `agentihooks mcp list --dir /path/to/mcp-library/`

Restart Claude Code after any install/uninstall for changes to take effect.

---

## Reproducing a setup on a new machine

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone the repo
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
cd agentihooks

# 3. Install dependencies
uv sync --all-extras

# 4. Copy your env file (from backup, 1Password, etc.)
cp /path/to/backup/.env ~/.agentihooks/.env

# 5. Install hooks, skills, agents, MCPs
uv run agentihooks global

# 6. Install the agentienv shell function + requirements
#    (activate a venv first so --loadenv can install packages)
python3 -m venv .venv && source .venv/bin/activate
agentihooks --loadenv   # writes function + auto-call, offers to install requirements.txt
source ~/.bashrc

# 7. Load env vars and launch Claude Code
agentienv && claude

# 8. Drop MCP files into ~/.agentihooks/ and install
cp /path/to/my-servers.json ~/.agentihooks/
agentihooks mcp install
```

Everything restored. No manual settings editing, no hunting for which keys go where.

---

## Sharing a setup within a team

1. Keep `.env.example` up to date in the repo with all variable names (no values)
2. Share values via a secrets manager (1Password, AWS Secrets Manager, Vault)
3. Each developer runs `agentihooks global` and populates `~/.agentihooks/.env`
4. Each developer runs `agentihooks --loadenv` to install the `agentienv` shell function
5. Keep curated `.mcp.json` files in a shared repo or distribute them to each developer
6. Each developer drops them into `~/.agentihooks/` and runs `agentihooks mcp install`

The repo stays credential-free. `~/.agentihooks/.env` is on each developer's machine only.
