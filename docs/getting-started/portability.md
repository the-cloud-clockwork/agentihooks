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
├── logs/               # Hook + MCP + daemon log files
└── memory/             # Per-project agent memory files
```

`agentihooks uninstall` never touches this directory -- your credentials and memory survive reinstalls.

To fully reset: `rm -rf ~/.agentihooks`

---

## Overriding the state directory (`AGENTIHOOKS_HOME`)

The state directory location is overridable via the `AGENTIHOOKS_HOME` env var. Set it before any `agentihooks` command and the entire stack — `install.py`, `sync_daemon.py`, every hook, the brain outbox, the MCP cache, session flags — resolves through that path.

```bash
export AGENTIHOOKS_HOME=/var/lib/agentihooks
agentihooks init
```

This was added for **per-pod isolation on shared filesystems**.

### Why per-pod isolation matters

When multiple agent pods (K8s StatefulSet, Docker Compose, etc.) share a single mount like `/shared/.agentihooks/`, they all write to the same `state.json`, the same session flag files, the same PID files. With 6+ pods running concurrently, the result is:

- **Race conditions** on `state.json` — last writer wins, install metadata gets pinned to whichever pod wrote last
- **Cross-pod state leaks** — a session flag from pod A appears as active in pod B
- **`--force` becomes destructive across the fleet** — wiping one pod's state takes down everyone's broadcasts and session caches

### The fix

Give each pod its own state directory by templating `AGENTIHOOKS_HOME` from a stable per-pod identifier:

```bash
# K8s StatefulSet — HOSTNAME is set automatically to <sts-name>-<ordinal>
export AGENTIHOOKS_HOME=/shared/.agentihooks-${HOSTNAME}
agentihooks init
```

Or with the Downward API:

```yaml
env:
  - name: POD_NAME
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
  - name: AGENTIHOOKS_HOME
    value: /shared/.agentihooks-$(POD_NAME)
```

Each pod now has an isolated state directory: `/shared/.agentihooks-diagram-agent-0/`, `/shared/.agentihooks-publisher-agent-0/`, etc. No races, no cross-pod state, `--force` is scoped to the calling pod only.

### Opt-in shared state

If you want a group of pods to share state (e.g. an orchestrator with worker subprocesses), point them all at the same value:

```bash
export AGENTIHOOKS_HOME=/shared/.agentihooks-orchestrator-group-A
```

All pods in the group converge on that path. Pods outside the group are unaffected.

### What `AGENTIHOOKS_HOME` does NOT change

- `~/.claude/` — that's the Claude Code config dir, separate concern (use `CLAUDE_CODE_HOME_DIR` for that)
- The bundle clone path — that's a separate `--bundle` argument
- The agentihooks repo clone path — that's wherever you cloned the repo

---

## Environment file (`~/.agentihooks/.env`)

All integration keys live in one place:

```bash
# MCP server credentials
MCP_ATLASSIAN_PROXY_API_KEY=...
MCP_SONAR_PROXY_API_KEY=...
MCP_AGENTIBRIDGE_API_KEY=...

# Service endpoints
LITELLM_URL=http://your-litellm-host:4000
GRAFANA_URL=http://your-grafana-host:3000

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
├── anton-mcp.env      # ANTON_HOST=your-anton-host, ANTON_API_KEY=...
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
git clone https://github.com/The-Cloud-Clockwork/agentihooks
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

## Clean reinstalls with `--force`

`agentihooks init --force` resets install state without nuking persistent data. Use it when state has drifted, the install metadata is stale, or you want a known-good baseline without losing broadcasts, enforcements, or brain data.

### What gets reset

| Reset | Reason |
|---|---|
| `state.json` | Install metadata — re-derived on init |
| `sync-hashes.json`, `sync.lock` | Sync daemon state — re-derived |
| `active-sessions.json`, `active_overlays.json` | Session registry — naturally repopulated |
| `mcp-tool-cache.json` | MCP cache — repopulated on first MCP call |
| `broadcast_delivery_state.json` | Per-session delivery tracking (broadcasts themselves survive) |
| `controls_flags/`, `voice_flags/`, `prod_bypass/`, `force_refresh/` | Per-session flags — naturally re-armed by signals |
| `state/` | Memory dirty markers — re-derived |
| `*.pid`, `ctx_refresh_*.json` | Stale daemon PIDs + session refresh signals |
| `~/.claude/{rules,skills,agents,commands}/` symlinks | Re-symlinked by init |
| `~/.claude/{settings.json,settings.local.json,CLAUDE.md}` | Regenerated by init |

### What survives

| Preserved | Reason |
|---|---|
| `.env`, `.venv` | Operator secrets + Python env (expensive rebuild) |
| `enforcements.json` | Operator-defined enforcement rules |
| `broadcast.json` | Active broadcasts |
| `enforcement_counters.json` | Per-session counters (reset naturally) |
| `quota-accounts/` | Quota tracking config |
| `known-mcp-servers.json` | MCP server registry |
| `memory-mirror/` | Local clone of memory repo |
| `brain-feed/`, `brain-outbox/`, `brain_adapter_hash.json` | Brain pipeline data |
| `logs/` | Diagnostic history |
| `agent_memories.jsonl` | Memory store |
| `claude_usage.json`, `claude_auth_state.json` | Usage tracking |
| `playwright_profile/` | Browser session state |
| `transcript_positions/` | Transcript read cursors |

### Usage

```bash
# Standard clean reinstall — keep operator data
agentihooks init --force

# Clean reinstall on a specific profile
agentihooks init --force --profile default

# Per-pod clean reinstall on shared FS
AGENTIHOOKS_HOME=/shared/.agentihooks-${HOSTNAME} agentihooks init --force
```

For a true full wipe (including `.env`), use `rm -rf ~/.agentihooks` — `--force` is intentionally narrower.

---

## Sharing a setup within a team

1. Keep `.env.example` up to date in the repo with all variable names (no values)
2. Share values via a secrets manager (1Password, AWS Secrets Manager, Vault)
3. Each developer runs `agentihooks init` and populates `~/.agentihooks/.env`
4. The bashrc block is written automatically -- just `source ~/.bashrc`
5. If using a bundle, each developer clones the bundle repo and runs `agentihooks init --bundle <path>`

The repo stays credential-free. `~/.agentihooks/.env` is on each developer's machine only.
