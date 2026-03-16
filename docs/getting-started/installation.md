---
title: Installation
nav_order: 2
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
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
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

Always run the installer **from the `~/.agentihooks/.venv` Python** — the installer bakes `sys.executable` into every hook command it writes.

```bash
~/.agentihooks/.venv/bin/python scripts/install.py global
```

This single command:

1. Reads `profiles/_base/settings.base.json` (the canonical settings source)
2. Substitutes `/app` placeholders with the real repo path and `__PYTHON__` with `~/.agentihooks/.venv/bin/python`
3. Writes `~/.claude/settings.json` with hook wiring and tool permissions
4. Symlinks skills, agents, and commands from `.claude/` into `~/.claude/`
5. Symlinks `~/.claude/CLAUDE.md` to the chosen profile's system prompt
6. Merges profile `.mcp.json` into `~/.claude.json` (user-scope MCP servers)
7. Installs the `agentihooks` CLI globally via `uv tool install --editable .`

The install is **idempotent** — re-running is safe. Settings are only backed up on the first run.

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
# Redis — enables burn rate tracking, file read cache, warning edge-triggers
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

## 7. (Optional) Console quota display

The statusline can show your Anthropic console usage (session %, weekly %, monthly spend) on line 3. This requires a one-time browser login via Playwright.

### Install Playwright's browser

```bash
~/.agentihooks/.venv/bin/python -m playwright install chromium
```

### First run — headed (browser opens for login)

```bash
~/.agentihooks/.venv/bin/python scripts/claude_usage_watcher.py --headed
```

A Chromium window opens. Log in to [claude.ai](https://claude.ai). After login, the watcher scrapes the usage page and writes `~/.agentihooks/claude_usage.json`, then exits. Your session is saved to `~/.agentihooks/playwright_profile/` — you will not need to log in again.

### Background watcher (headless, every 60 s)

```bash
nohup ~/.agentihooks/.venv/bin/python scripts/claude_usage_watcher.py \
  >> ~/.agentihooks/logs/watcher.log 2>&1 &
```

### Enable the statusline display

Add to `~/.agentihooks/.env`:

```bash
CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json
# CLAUDE_USAGE_STALE_SEC=300   # mark data stale after 5 min (default)
# CLAUDE_USAGE_POLL_SEC=60     # watcher poll interval (default)
```

The statusline will then display e.g. `s:9% w:29% €40/100 [3h]` on line 3, color-coded by usage thresholds.

### Auto-start on login (optional)

Add to `~/.bashrc` or `~/.profile`:

```bash
# Start agentihooks quota watcher if not already running
if ! pgrep -f "claude_usage_watcher.py" > /dev/null; then
  nohup ~/.agentihooks/.venv/bin/python \
    /path/to/agentihooks/scripts/claude_usage_watcher.py \
    >> ~/.agentihooks/logs/watcher.log 2>&1 &
fi
```

---

## Using a specific profile

```bash
~/.agentihooks/.venv/bin/python scripts/install.py global --profile coding
```

Or use the `AGENTIHOOKS_PROFILE` environment variable:

```bash
export AGENTIHOOKS_PROFILE=coding
~/.agentihooks/.venv/bin/python scripts/install.py global
```

List available profiles:

```bash
~/.agentihooks/.venv/bin/python scripts/install.py global --list-profiles
```

---

## Install MCP tools into a specific project

To wire the MCP server for a single project (without global install):

```bash
~/.agentihooks/.venv/bin/python scripts/install.py project ~/dev/my-project
```

This writes a `.mcp.json` into the target project directory.

---

## Standalone MCP server

Run the MCP server directly (useful for testing):

```bash
# All 45 tools
~/.agentihooks/.venv/bin/python -m hooks.mcp

# Specific categories only
MCP_CATEGORIES=github,utilities ~/.agentihooks/.venv/bin/python -m hooks.mcp
```

---

## Custom Claude config directory

By default the installer targets `~/.claude`. If `$HOME` differs from where
Claude Code actually stores its config, set `CLAUDE_CODE_HOME_DIR` to the
correct home-directory root — agentihooks appends `.claude` automatically:

```bash
CLAUDE_CODE_HOME_DIR=/shared/home \
  ~/.agentihooks/.venv/bin/python scripts/install.py global
# installs into /shared/home/.claude/
```

The legacy `AGENTIHOOKS_CLAUDE_HOME` still works and points directly at the
`.claude` directory (no `.claude` appended). Priority order:

1. `CLAUDE_CODE_HOME_DIR` (home-dir root, `.claude` appended)
2. `AGENTIHOOKS_CLAUDE_HOME` (direct `.claude` path, legacy)
3. `~/.claude` (default)

---

## Auto-merge an MCP file during install

Set `AGENTIHOOKS_MCP_FILE` to have the installer automatically merge an external MCP file into `~/.claude.json`:

```bash
export AGENTIHOOKS_MCP_FILE=/shared/gateway-mcp.json
~/.agentihooks/.venv/bin/python scripts/install.py global
```

The path is recorded in `state.json` so future installs re-apply it automatically.

---

## Uninstall

To remove everything agentihooks installed:

```bash
agentihooks uninstall
```

Add `--yes` to skip the confirmation prompt.

{: .warning }
This removes hooks, skills, agents, CLAUDE.md, and MCP server registrations. User data in `~/.agentihooks/` is **not** removed — delete it manually with `rm -rf ~/.agentihooks` if desired.
