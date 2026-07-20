# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# The canonical Python for this project lives at ~/.agentihooks/.venv
# Always use `uv run` or the venv Python so hooks and tests run against the same packages.

uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"   # install/update deps
uv run python -m pytest                                                # run all tests
uv run python -m pytest tests/test_hook_manager.py                     # single file
uv run python -m pytest tests/test_config.py::TestSecretsMode -v       # single test
uv run ruff check .                                                    # lint
uv run ruff format .                                                   # format
agentihooks init --profile anton                                       # global install
```

## The Four Pillars

AgentiHooks is organized around four pillars. When working on this codebase, understand which pillar a change affects:

| Pillar | Core files | What it does |
|--------|-----------|-------------|
| **Identity** | `scripts/install.py`, `profiles/`, `settings.base.json` | Profile system, chaining, two-axis model, bundle merge |
| **Guardrails** | `hooks/secrets.py`, `hooks/context/retry_breaker.py`, `hooks/context/branch_guard.py`, `hooks/context/prod_lockdown.py`, `hooks/context/ci_manifesto.py`, `hooks/context/dep_banner.py`, `hooks/context/_strip.py`, `hooks/context/version_guard.py`, `hooks/context/claude_md_sanity.py` | Two-tier secrets, retry breaker, branch/PR gating, prod lockdown, CI manifesto signal parsing, dep install banner, shared command stripping, version guard, CLAUDE.md bloat guard |
| **Context Intelligence** | `hooks/context/preprocessor.py`, `hooks/context/brain_adapter.py`, `hooks/context/rules_refresh.py`, `hooks/tool_memory.py` | Token compression, brain injection, one-shot rule refresh to running sessions, tool memory |
| **Fleet Command** | `hooks/context/broadcast.py`, `hooks/mcp/channels.py`, broadcast sections in `hook_manager.py`, CLI in `install.py` | Real-time messaging with channel-based targeting, brain adapter |

## Architecture

### Entry points

- **`agentihooks` CLI** → `scripts/install.py:main()` — installs hooks/settings/MCPs, manages profiles/bundles, broadcast CLI
- **Lifecycle hooks** → all 10 hook events point to `python -m hooks` → `hooks/hook_manager.py:main()`
- **StatusLine** → `hooks/statusline.py` — 2-3 line status bar (not a hook event)
- **MCP tools** → `hooks/mcp/` — separate process registered as `hooks-utils`

### Hook dispatch

`hook_manager.py:main()` reads JSON from stdin, routes to `EVENT_HANDLERS[event_name](payload)`:

| Event | Handler | Key behavior |
|---|---|---|
| `SessionStart` | `on_session_start` | Register broadcast session, inject context, brain injection, MCP warning |
| `SessionEnd` | `on_session_end` | Deregister session, clear caches, log summary |
| `UserPromptSubmit` | `on_user_prompt_submit` | Secrets scan, brain refresh, CI-manifesto/enforcement drumbeat, channel-filtered broadcast delivery |
| `PreToolUse` | `on_pre_tool_use` | Secrets scan, guardrails pipeline, critical broadcast via additionalContext |
| `PostToolUse` | `on_post_tool_use` | Bash filter, file dedup, tool error recording |
| `Stop` / `SubagentStop` | `on_stop` | Memory auto-save, cost logging |

`BlockAction` is caught at `main()` top → stderr + exit 2 → Claude Code reads the block reason.

### Configuration

`hooks/config.py` auto-loads `~/.agentihooks/.env` + `~/.agentihooks/*.env` at import time. All feature flags are env vars.

### Settings installation

`scripts/install.py` reads `settings.base.json`, substitutes `__PYTHON__`/`/app` placeholders, deep-merges profile overrides + settings-profile overlay, writes to `~/.claude/settings.json`.

### Profile system

3-layer merge: agentihooks built-in → bundle global → profile-specific. Profiles chained with commas. Two-axis model: persona (rules/CLAUDE.md) independent from settings (permissions/MCP).

### Broadcast system + channels

File-based pub/sub at `~/.agentihooks/broadcast.json`. Sessions auto-register/deregister via hooks. Three severity tiers (info/alert/critical). AI-assisted `emit` spawns sandboxed Haiku (Bash(agentihooks*) only).

**Channels:** Messages can have an optional `channel` field. Subscriptions are env-driven via `AGENTIHOOKS_BASE_CHANNELS` (comma-separated). Default ships in `profiles/default/.claude/settings.overrides.json` `env` block as `"brain,amygdala"`. Layering: profile env → repo `.claude/settings.json` → repo `.claude/settings.local.json` → container ENV at launch (highest). Empty / unset → session only receives global broadcasts (messages with no `channel` field).

### Brain adapter

`hooks/context/brain_adapter.py` bridges an external knowledge source (file/vault/API) to the broadcast channel system. Pluggable `BrainSource` interface; ships with `FileBrainSource` reading `~/.agentihooks/brain/*.md` (YAML frontmatter + markdown body). Counter-gated refresh every N turns. Publishes to the `brain` broadcast channel. Config: `BRAIN_ENABLED`, `BRAIN_SOURCE_PATH`, `BRAIN_CHANNEL`, `BRAIN_REFRESH_INTERVAL`.

### Testing patterns

Tests mock Redis via `patch("hooks._redis.get_redis", return_value=None)`. Pre-existing failures in `TestBranchGuard` and `TestActiveProfileDetection` are known. Use `uv run` for all test/lint commands.
