# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# The canonical Python for this project lives at ~/.agentihooks/.venv
# Always use it so hooks and tests run against the same packages that Claude Code fires.

# Install / update all dependencies
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"

# Run all tests
~/.agentihooks/.venv/bin/python -m pytest

# Run a single test file
~/.agentihooks/.venv/bin/python -m pytest tests/test_hook_manager.py

# Run a single test by name
~/.agentihooks/.venv/bin/python -m pytest tests/test_config.py::TestSecretsMode::test_secrets_mode_default -v

# Run tests with coverage
~/.agentihooks/.venv/bin/python -m pytest --cov=hooks

# Lint
~/.agentihooks/.venv/bin/python -m ruff check .

# Install agentihooks globally (hooks + settings + MCP into ~/.claude)
# Must be run from the venv Python — bakes sys.executable into hook commands in settings.json
~/.agentihooks/.venv/bin/python scripts/install.py global

# Install into a specific project
~/.agentihooks/.venv/bin/python scripts/install.py project /path/to/repo

# Manage MCP server files interactively
agentihooks mcp install
agentihooks mcp list
```

## Architecture

### Python environment

`~/.agentihooks/.venv` is the canonical venv for this project. All hook commands in `~/.claude/settings.json` point to its Python binary. This guarantees that Claude Code's hook subprocesses find the correct packages no matter which shell, terminal, or activated venv the user has when they launch `claude`.

**Why this matters:** Hook commands are shell strings stored in `settings.json`. They run as subprocesses spawned by Claude Code — outside any current shell's `VIRTUAL_ENV`. If the wrong Python is embedded, imports fail silently. The installer writes `sys.executable` into every hook command, so running the install from `~/.agentihooks/.venv/bin/python` is the single step that wires everything correctly.

### Entry points

- **`agentihooks` CLI** → `scripts/install.py:main()` — installs hooks/settings/MCPs, manages MCP server files, handles `--loadenv`
- **Lifecycle hooks** → all 10 Claude Code hook events point to `~/.agentihooks/.venv/bin/python -m hooks` → `hooks/__main__.py` → `hooks/hook_manager.py:main()`
- **StatusLine** → `hooks/statusline.py` — reads JSON from stdin each turn, outputs 2–3 line status bar (not a hook event; configured via `settings.json` `statusLine` key)
- **Quota watcher** → `scripts/claude_usage_watcher.py` — async Playwright daemon; scrapes claude.ai/settings/usage, writes `~/.agentihooks/claude_usage.json`

### How hooks dispatch

`hook_manager.py:main()` reads the JSON payload from stdin, extracts `hook_event_name`, and routes to `EVENT_HANDLERS[event_name](payload)`. The handlers are:

| Event | Handler | Key behavior |
|---|---|---|
| `SessionStart` | `on_session_start` | Injects context (token limit, working dir), MCP hygiene message, logs max output tokens |
| `SessionEnd` | `on_session_end` | Saves memory, clears file read cache, logs session summary |
| `UserPromptSubmit` | `on_user_prompt_submit` | Injects tool memory context panel |
| `PreToolUse` | `on_pre_tool_use` | Secrets scanning, file read cache block (raises `BlockAction` → exit 2 → stderr) |
| `PostToolUse` | `on_post_tool_use` | Bash output filtering, marks files as read in cache, transcript logging |
| `Stop` / `SubagentStop` | `on_stop` / `on_subagent_stop` | Memory auto-save, cost logging |

`BlockAction` is caught at the top of `main()`, prints its message to **stderr**, then exits 2 — Claude Code reads stderr for the block reason.

### Configuration loading

`hooks/config.py` auto-loads at import time:
1. `~/.agentihooks/.env` (always first)
2. `~/.agentihooks/*.env` (alphabetically — companion files for MCP JSON bundles)

All token control, Redis, and feature flags are read from these files. There is no separate config file format — everything goes in `.env` files in `~/.agentihooks/`.

### Settings installation flow

`scripts/install.py` reads `profiles/_base/settings.base.json` (the canonical settings source), substitutes `__PYTHON__` with `sys.executable` (the Python that ran the installer) and `/app` with the repo root, then deep-merges into `~/.claude/settings.json`. The `_managedBy` marker prevents re-running from overwriting personal settings keys. `~/.claude.json` gets MCP server entries merged in.

### Profile system

Profiles live in `profiles/<name>/` and contain:
- `profile.yml` — metadata + `mcp_categories` field
- `.claude/CLAUDE.md` — symlinked to `~/.claude/CLAUDE.md` on install

`_base` is not a profile; it holds `settings.base.json` which every install derives from. Current profiles: `default`, `admin`, `coding`.

### Token Control Layer

Three subsystems, all gated by `TOKEN_CONTROL_ENABLED`:

- **`hooks/observability/token_monitor.py`** — computes `fill_pct` and `burn_rate` (delta from previous turn), persists to Redis `agenticore:tokens:{session_id}`. `should_warn_context()` is edge-triggered (one warn per threshold crossing per session via `agenticore:token_warn:{session_id}`).
- **`hooks/context/bash_output_filter.py`** — detects docker/kubectl/git-log/test-runner/build output by command string, truncates with notices. Returns `None` if output is already under limits (no unnecessary modification).
- **`hooks/context/file_read_cache.py`** — Redis Set + mtime Hash per session (`agenticore:file_cache:{sid}`, `agenticore:file_mtime:{sid}`). `check_and_block_redundant_read()` raises `BlockAction` if a file was already read and hasn't changed on disk since. Falls back to in-memory dict when Redis is unavailable.

### Console Quota Display (opt-in)

`hooks/quota.py` reads a JSON file written by `scripts/claude_usage_watcher.py` and surfaces Anthropic console usage (session %, weekly %, monthly spend) on statusline line 3.

The watcher is a Playwright async daemon with a persistent browser context at `~/.agentihooks/playwright_profile/`. First-run requires `--headed` for login; subsequent runs are headless.

```bash
# One-time: install the browser
~/.agentihooks/.venv/bin/python -m playwright install chromium

# First run: headed login (opens Chromium, log in to claude.ai)
~/.agentihooks/.venv/bin/python scripts/claude_usage_watcher.py --headed

# Background daemon
nohup ~/.agentihooks/.venv/bin/python scripts/claude_usage_watcher.py \
  >> ~/.agentihooks/logs/watcher.log 2>&1 &
```

Enable in `~/.agentihooks/.env`:
```bash
CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json
```

### Redis

`hooks/_redis.py` provides `get_redis()` (lazy singleton, returns `None` on any connection failure) and `redis_key(type, id)` which prefixes with `agenticore:`. All Redis usage degrades gracefully — features still work without it, just with less persistence.

### MCP tool server

`hooks/mcp/` is a separate MCP server (`python -m hooks.mcp`, registered as `hooks-utils`). It exposes tools grouped by `MCP_CATEGORIES` env var. Separate from the lifecycle hooks — different process, different entry point.

### Path placeholder convention

Hook commands in `settings.base.json` use `/app` and `__PYTHON__` as placeholders. `scripts/install.py:substitute_paths()` replaces them at install time. `build_profiles.py` (Docker only) keeps `/app` literal.

### Testing patterns

Tests mock Redis via `patch("hooks._redis.get_redis", return_value=None)` to avoid external dependencies. Most tests use `pytest.mark.unit`. Pre-existing failures in `TestSecretsModesIntegration` and `TestBlockActionIntegration` are known and unrelated to new work.
