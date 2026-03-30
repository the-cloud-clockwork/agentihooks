---
title: Portability & Reusability
nav_order: 4
parent: Getting Started
---

# Portability & Reusability
{: .no_toc }

AgentiHooks is designed to travel with you. One data directory, one env file, and an idempotent install command let you reproduce a complete Claude Code environment on any machine -- or share a setup across a team.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The `~/.agentihooks/` data directory

Everything user-specific lives in a single directory:

```
~/.agentihooks/
├── .env                # Main credentials (always loaded first)
├── *.env               # Companion env files (auto-sourced alphabetically after .env)
├── state.json          # Tracks installed state, bundle path, targets
├── quota-accounts/     # Multi-account quota credentials
│   ├── work.json
│   └── personal.json
├── logs/               # Hook + MCP + daemon log files
└── memory/             # Per-project agent memory files
```

`agentihooks uninstall` never touches this directory -- your credentials and memory survive reinstalls.

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

The file is seeded from `.env.example` on first `agentihooks init` and is **never overwritten** on subsequent runs.

**To move to a new machine:** copy `~/.agentihooks/.env` alongside the repo clone.

---

## Loading env vars into your shell

Claude Code expands `${VAR}` in MCP configs from its own process environment at startup. `agentihooks init` automatically writes a managed block to `~/.bashrc` that defines the `agentienv` shell function and the `agenti` alias. The function is auto-called in every new shell so vars are loaded automatically.

### What init writes to ~/.bashrc

A managed block between `# === agentihooks ===` and `# === end-agentihooks ===` markers:

```bash
# === agentihooks ===
agentienv() {
  # sources ~/.agentihooks/.env first, then all *.env files alphabetically
  ...
}
alias agenti='agentihooks claude'
agentienv
# === end-agentihooks ===
```

The `agentienv()` function:
1. Sources `~/.agentihooks/.env`
2. Sources all `*.env` files alphabetically from the same directory
3. Reports how many files were loaded

The trailing `agentienv` call means vars are loaded automatically in every new shell -- no manual invocation needed unless you add new env files mid-session.

The block is **idempotent** -- re-running `agentihooks init` updates the block in place rather than appending. Keep your own aliases outside the markers.

### Usage

```bash
# After first install, reload your shell
source ~/.bashrc

# Vars are already loaded automatically. Call agentienv manually only
# if you add new env files mid-session:
agentienv

# Launch claude with profile flags
agenti
```

Then launch `claude` (or `agenti`) from that shell -- all `${VAR}` placeholders in your MCP configs resolve correctly.

### Why this exists

Claude Code expands `${VAR}` in MCP server configs from its own process environment at startup. Variables defined only in hook subprocesses arrive too late. `agentienv` loads them into the launching shell so `claude` inherits them.

---

## Companion `.env` files

Drop a `.env` file alongside your MCP JSON to provide the env vars it needs:

```
~/.agentihooks/
├── anton-mcp.json     # MCP server definitions (${ANTON_HOST}, etc.)
├── anton-mcp.env      # ANTON_HOST=10.10.30.130, ANTON_API_KEY=...
├── staging.json
└── staging.env
```

Companion env files are auto-sourced by:
- **Hook runtime** -- `hooks/config.py` loads `~/.agentihooks/.env` first, then all `*.env` files alphabetically
- **`agentienv` shell function** -- sources `.env` then all `*.env` files so `${VAR}` placeholders in MCP configs resolve at Claude Code startup

---

## Reproducing a setup on a new machine

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone the repo
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
cd agentihooks

# 3. Create the venv and install
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"

# 4. Copy your env file (from backup, 1Password, etc.)
cp /path/to/backup/.env ~/.agentihooks/.env

# 5. Install everything (hooks, skills, agents, MCPs, bashrc, daemons)
agentihooks init

# 6. Reload shell to pick up agentienv + agenti alias
source ~/.bashrc

# 7. Launch Claude Code
agenti
```

Everything restored. No manual settings editing, no hunting for which keys go where.

If you use a bundle:

```bash
# Clone your tools repo and install with bundle
git clone https://github.com/you/my-tools ~/dev/my-tools
agentihooks init --bundle ~/dev/my-tools --profile coding
source ~/.bashrc
```

---

## Sharing a setup within a team

1. Keep `.env.example` up to date in the repo with all variable names (no values)
2. Share values via a secrets manager (1Password, AWS Secrets Manager, Vault)
3. Each developer runs `agentihooks init` and populates `~/.agentihooks/.env`
4. The bashrc block is written automatically -- just `source ~/.bashrc`
5. If using a bundle, each developer clones the bundle repo and runs `agentihooks init --bundle <path>`

The repo stays credential-free. `~/.agentihooks/.env` is on each developer's machine only.
