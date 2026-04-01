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
agentihooks init --profile colt

# Install into a specific project
agentihooks init --repo /path/to/repo
```

## Architecture

### Python environment

`~/.agentihooks/.venv` is the canonical venv for this project. All hook commands in `~/.claude/settings.json` point to its Python binary. This guarantees that Claude Code's hook subprocesses find the correct packages no matter which shell, terminal, or activated venv the user has when they launch `claude`.

**Why this matters:** Hook commands are shell strings stored in `settings.json`. They run as subprocesses spawned by Claude Code — outside any current shell's `VIRTUAL_ENV`. If the wrong Python is embedded, imports fail silently. The installer writes `sys.executable` into every hook command, so running the install from `~/.agentihooks/.venv/bin/python` is the single step that wires everything correctly.

### Entry points

- **`agentihooks` CLI** → `scripts/install.py:main()` — installs hooks/settings/MCPs, manages MCP server files, manages profiles/bundles
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

Profiles mirror the Claude Code project structure:
```
profiles/<name>/
├── CLAUDE.md                    # system prompt (→ ~/.claude/CLAUDE.md)
├── profile.yml                  # agentihooks metadata
└── .claude/
    ├── settings.overrides.json  # merged into ~/.claude/settings.json
    ├── .mcp.json                # profile MCP servers
    ├── skills/                  # → ~/.claude/skills/
    ├── agents/                  # → ~/.claude/agents/
    ├── commands/                # → ~/.claude/commands/
    └── rules/                   # → ~/.claude/rules/
```

3-layer merge: agentihooks built-in → bundle global → profile-specific.

`_base` is not a profile; it holds `settings.base.json` which every install derives from. Current profiles: `default`, `admin`, `coding`.

### Token Control Layer

Three subsystems, all gated by `TOKEN_CONTROL_ENABLED`:

- **`hooks/observability/token_monitor.py`** — computes `fill_pct` and `burn_rate` (delta from previous turn), persists to Redis `agenticore:tokens:{session_id}`. `should_warn_context()` is edge-triggered (one warn per threshold crossing per session via `agenticore:token_warn:{session_id}`).
- **`hooks/context/bash_output_filter.py`** — detects docker/kubectl/git-log/test-runner/build output by command string, truncates with notices. Returns `None` if output is already under limits (no unnecessary modification).
- **`hooks/context/file_read_cache.py`** — Redis Set + mtime Hash per session (`agenticore:file_cache:{sid}`, `agenticore:file_mtime:{sid}`). `check_and_block_redundant_read()` raises `BlockAction` if a file was already read and hasn't changed on disk since. Falls back to in-memory dict when Redis is unavailable.

### Console Quota Display (opt-in)

`hooks/quota.py` reads a JSON file written by `scripts/claude_usage_watcher.py` and surfaces Anthropic console usage on statusline line 3. Example output:

```
session:53% [1h] | all:35% resets fri 10:00 am | sonnet:5% resets mon 12:00 am | extra: €40/99 (40%) resets apr 1
```

The watcher (`scripts/claude_usage_watcher.py`) is a headless Playwright daemon that scrapes claude.ai/settings/usage. Auth uses your real browser — no Chromium login flow. The CLI manages the daemon lifecycle:

```bash
# Install Playwright's browser (one-time)
~/.agentihooks/.venv/bin/python -m playwright install chromium

# Multi-account: add accounts by name
agentihooks quota auth personal    # add/update "personal" account
agentihooks quota auth team        # add "team" account

# Manage accounts
agentihooks quota list             # show all accounts, mark active
agentihooks quota switch           # interactive picker
agentihooks quota switch team      # switch + restart daemon

# Daemon commands
agentihooks quota                  # status if running, start if not
agentihooks quota restart          # stop + start
agentihooks quota status           # full quota JSON
agentihooks quota logs             # tail -f daemon log
agentihooks quota stop             # kill daemon
agentihooks quota remove <name>    # delete account
```

Auth flow: `quota auth <name>` opens the system browser to claude.ai. User copies the `sessionKey` cookie from Chrome DevTools (F12 → Application → Cookies → claude.ai → sessionKey). The cookie is saved to `~/.agentihooks/quota-accounts/<name>.json`. Active account tracked in `state.json`.

Enable in `~/.agentihooks/.env`:
```bash
CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json
# CLAUDE_USAGE_STALE_SEC=300   # data older than this shows "stale" (default)
# CLAUDE_USAGE_POLL_SEC=60     # daemon poll interval (default)
```

### Sync Daemon (auto-propagation)

`scripts/sync_daemon.py` watches source files and auto-propagates changes to all registered targets. Each `agentihooks init` registers the target in `state.json`. The daemon hashes sources (SHA-256), detects changes per category (`base`, `profile:{name}`, `connector`, `mcp_files`, `env`, `bundle`), and re-runs the install pipeline for affected targets. Uses `~/.agentihooks/sync.lock` (advisory flock) for concurrency.

```bash
agentihooks daemon              # start (default 60s poll)
agentihooks daemon status|logs|stop
agentihooks daemon start --poll 30 --foreground   # custom interval / debug
```

### CLAUDE.md Sanity Check

`hooks/context/claude_md_sanity.py` — PreToolUse guardrail that blocks Write/Edit operations on `CLAUDE.md` and `CLAUDE.local.md` if the resulting file would exceed a configurable line limit. Enabled by default.

| Env var | Default | Description |
|---|---|---|
| `AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK` | `true` | Enable/disable the guardrail (0/1) |
| `AGENTIHOOKS_CLAUDE_MD_MAXLINES` | `200` | Max allowed lines in CLAUDE.md files |

On violation: raises `BlockAction` (exit 2) with a message telling the agent the current/resulting line count and the cap. The agent must trim the file before retrying.

### Redis

`hooks/_redis.py` provides `get_redis()` (lazy singleton, returns `None` on any connection failure) and `redis_key(type, id)` which prefixes with `agenticore:`. All Redis usage degrades gracefully — features still work without it, just with less persistence.

### MCP tool server

`hooks/mcp/` is a separate MCP server (`python -m hooks.mcp`, registered as `hooks-utils`). It exposes tools grouped by `MCP_CATEGORIES` env var. Separate from the lifecycle hooks — different process, different entry point.

### Path placeholder convention

Hook commands in `settings.base.json` use `/app` and `__PYTHON__` as placeholders. `scripts/install.py:substitute_paths()` replaces them at install time. `build_profiles.py` (Docker only) keeps `/app` literal.

### Testing patterns

Tests mock Redis via `patch("hooks._redis.get_redis", return_value=None)` to avoid external dependencies. Most tests use `pytest.mark.unit`. Pre-existing failures in `TestSecretsModesIntegration` and `TestBlockActionIntegration` are known and unrelated to new work.
