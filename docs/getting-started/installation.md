---
title: Installation
nav_order: 2
parent: Getting Started
---

# Installation
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## 1. Install uv

AgentiHooks uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify:

```bash
uv --version
```

---

## 2. Clone the repository

```bash
git clone https://github.com/The-Cloud-Clockwork/agentihooks
cd agentihooks
```

---

## 3. Create the dedicated venv

AgentiHooks uses a fixed venv at `~/.agentihooks/.venv` as its canonical Python environment. All hook commands written into `~/.claude/settings.json` point to this venv's Python, so every hook subprocess finds the right packages regardless of which shell, activated venv, or terminal Claude Code is launched from.

```bash
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"
```

The `[all]` extra pulls in every optional dependency: `boto3`, `psycopg2`, `redis`, `pyyaml`, `playwright`, and others.

Verify:

```bash
~/.agentihooks/.venv/bin/python -c "import hooks; print('OK')"
```

---

## 4. Run the global install

Always run the installer **from the `~/.agentihooks/.venv` Python** -- the installer bakes `sys.executable` into every hook command it writes.

```bash
agentihooks init
```

This single command:

1. Reads `profiles/_base/settings.base.json` (the canonical settings source)
2. Merges settings: base -> profile `.claude/settings.overrides.json` -> OTEL
3. Substitutes `/app` placeholders with the real repo path and `__PYTHON__` with `~/.agentihooks/.venv/bin/python`
4. Writes `~/.claude/settings.json` with hook wiring and tool permissions
5. Symlinks skills, agents, commands, and rules via 3-layer merge (agentihooks built-in -> bundle global -> profile-specific)
6. Symlinks `~/.claude/CLAUDE.md` to the chosen profile's `CLAUDE.md` (at profile root)
7. Installs MCPs (hooks-utils + bundle `.claude/.mcp.json` + profile `.claude/.mcp.json`)
8. Reconciles the managed-MCP ledger in `~/.claude.json` — removes servers agentihooks installed on a prior run but that are no longer defined in any profile/bundle source (servers you added by hand are preserved; run `agentihooks prune` to sweep genuine cruft)
10. Installs the `agentihooks` CLI globally via `uv tool install --editable .`
11. Writes a managed bashrc block (`agentienv` function + `agenti` alias)

The install is **idempotent** -- re-running is safe. Settings are only backed up on the first run.

{: .note }
If you ever recreate the venv or change its location, re-run the installer from the new Python. The hook commands in `settings.json` will be updated automatically.

---

## 5. Verify

Confirm hook commands point to the venv:

```bash
grep -o '"command": "[^"]*"' ~/.claude/settings.json | head -3
# Should show: ~/.agentihooks/.venv/bin/python -m hooks
```

Confirm the MCP server is registered:

```bash
cat ~/.claude.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(list(d.get('mcpServers',{}).keys()))"
```

Start a Claude Code session and verify by asking:

```
What MCP tools do you have available?
```

The agent should list tools from agentihooks (e.g., `hooks_list_tools`, `get_env`, etc.).

---

## 6. Configure ~/.agentihooks/.env

On first install, `~/.agentihooks/.env` is seeded from `.env.example`. Edit it to configure integrations. Minimum recommended settings:

```bash
# Redis -- enables burn rate tracking, file read cache, warning edge-triggers
REDIS_URL=redis://:PASSWORD@host:port/0

# Token control
TOKEN_CONTROL_ENABLED=true
TOKEN_WARN_PCT=60
TOKEN_CRITICAL_PCT=80
BASH_FILTER_ENABLED=true
FILE_READ_CACHE_ENABLED=true
MCP_HYGIENE_ENABLED=true
MEMORY_AUTO_SAVE=true
```

All hooks and the MCP server auto-load this file at import time (plus any `~/.agentihooks/*.env` companion files).

---

## 7. Rate limit display

The statusline automatically shows your Claude Code rate limits on line 3 using native data from Claude Code:

```
session:53% [1h35m] | weekly:35%
```

No configuration required -- this works out of the box. Color-coded by usage: green < 60%, yellow < 80%, red above.

---

## Using a specific profile

```bash
agentihooks init --profile coding
```

Or use the `AGENTIHOOKS_PROFILE` environment variable:

```bash
export AGENTIHOOKS_PROFILE=coding
agentihooks init
```

List available profiles:

```bash
agentihooks --list-profiles
```

---

## Using a bundle

Link your external customization bundle on first install:

```bash
agentihooks init --bundle ~/dev/my-tools
```

After linking, future runs of `agentihooks init` will use the linked bundle automatically. See [Bundles](../bundles.md) for details.

---

## Standalone MCP server

Run the MCP server directly (useful for testing):

```bash
# All 26 tools
~/.agentihooks/.venv/bin/python -m hooks.mcp

# Specific categories only
MCP_CATEGORIES=aws,utilities ~/.agentihooks/.venv/bin/python -m hooks.mcp
```

---

## Custom Claude config directory

By default the installer targets `~/.claude`. If `$HOME` differs from where
Claude Code actually stores its config, set `CLAUDE_CODE_HOME_DIR` to the
correct home-directory root -- agentihooks appends `.claude` automatically:

```bash
CLAUDE_CODE_HOME_DIR=/shared/home \
  agentihooks init
# installs into /shared/home/.claude/
```

The legacy `AGENTIHOOKS_CLAUDE_HOME` still works and points directly at the
`.claude` directory (no `.claude` appended). Priority order:

1. `CLAUDE_CODE_HOME_DIR` (home-dir root, `.claude` appended)
2. `AGENTIHOOKS_CLAUDE_HOME` (direct `.claude` path, legacy)
3. `~/.claude` (default)

---

## Uninstall

To remove everything agentihooks installed:

```bash
agentihooks uninstall
```

Add `--yes` to skip the confirmation prompt.

This removes: settings, all symlinks, CLAUDE.md, MCP server registrations, the bashrc block, and the CLI. User data in `~/.agentihooks/state.json` is preserved.

{: .warning }
To fully remove all user data, delete `~/.agentihooks` manually with `rm -rf ~/.agentihooks`.
