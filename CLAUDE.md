# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync --all-extras

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_hook_manager.py

# Run a single test by name
uv run pytest tests/test_config.py::TestSecretsMode::test_secrets_mode_default -v

# Run tests with coverage
uv run pytest --cov=hooks

# Lint
uv run ruff check .

# Install agentihooks globally (hooks + settings + MCP into ~/.claude)
uv run agentihooks global

# Install into a specific project
uv run agentihooks project /path/to/repo

# Manage MCP server files interactively
uv run agentihooks mcp install
uv run agentihooks mcp list
```

## Architecture

### Entry points

- **`agentihooks` CLI** → `scripts/install.py:main()` — installs hooks/settings/MCPs, manages MCP server files, handles `--loadenv`
- **Lifecycle hooks** → all 10 Claude Code hook events point to `python -m hooks` → `hooks/__main__.py` → `hooks/hook_manager.py:main()`
- **StatusLine** → `hooks/statusline.py` — reads JSON from stdin each turn, outputs 2–3 line status bar (not a hook event; configured via `settings.json` `statusLine` key)

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

`scripts/install.py` reads `profiles/_base/settings.base.json` (the canonical settings source), substitutes `__PYTHON__` with the venv's python path and `/app` with the repo root, then deep-merges into `~/.claude/settings.json`. The `_managedBy` marker prevents re-running from overwriting personal settings keys. `~/.claude.json` gets MCP server entries merged in.

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

### Redis

`hooks/_redis.py` provides `get_redis()` (lazy singleton, returns `None` on any connection failure) and `redis_key(type, id)` which prefixes with `agenticore:`. All Redis usage degrades gracefully — features still work without it, just with less persistence.

### MCP tool server

`hooks/mcp/` is a separate MCP server (`python -m hooks.mcp`, registered as `hooks-utils`). It exposes tools grouped by `MCP_CATEGORIES` env var. Separate from the lifecycle hooks — different process, different entry point.

### Path placeholder convention

Hook commands in `settings.base.json` use `/app` and `__PYTHON__` as placeholders. `scripts/install.py:substitute_paths()` replaces them at install time. `build_profiles.py` (Docker only) keeps `/app` literal.

### Testing patterns

Tests mock Redis via `patch("hooks._redis.get_redis", return_value=None)` to avoid external dependencies. Most tests use `pytest.mark.unit`. Pre-existing failures in `TestSecretsModesIntegration` and `TestBlockActionIntegration` are known and unrelated to new work.
