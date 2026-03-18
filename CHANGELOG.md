# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`agentihooks mcp` two-stage interactive flow** — `mcp install` and `mcp uninstall` now use a two-stage UX: Stage 1 picks a file (auto-displayed if only one exists; numbered list with `•` bullet-point server names otherwise); Stage 2 picks which servers to install/remove (`0`=all, `N`=specific, comma-separated). A file is removed from tracking on uninstall only if all its servers were removed.
- **`agentihooks mcp list`** — servers are now displayed as `•` bullet points instead of a count string.
- **Token Control Layer** — new subsystem in `v0.3.0+` targeting 30–50% token reduction in agentic sessions. All features are individually disableable:
  - `hooks/observability/token_monitor.py` — tracks context fill % and burn rate per session via Redis (`agenticore:tokens:{session_id}`); edge-triggers threshold warnings via `agenticore:token_warn:{session_id}`
  - `hooks/context/bash_output_filter.py` — truncates verbose docker/kubectl/git-log/test/build output before it accumulates in the context window
  - `hooks/context/file_read_cache.py` — uses a Redis Set + mtime hash per session; blocks redundant re-reads with `BlockAction` (exit code 2 to **stderr**)
  - All wired in `hook_manager.py`: PreToolUse (file cache block), PostToolUse (bash filter + mark read), SessionStart (MCP hygiene inject), SessionEnd (cache clear)
- **`statusLine` native setting** — `settings.base.json` now includes `"statusLine": {"type": "command", "command": "cd /app && __PYTHON__ -m hooks.statusline"}`. `hooks/statusline.py` reads JSON from stdin and outputs a 2-line status bar (fill bar, model, cost, duration; then token counts, burn rate, lines changed, cache ratio, git branch) plus an optional threshold warning line.
- **`used_pct` recomputation fix** — `hooks/statusline.py` recomputes `used_pct` from `total_input_tokens / context_window_size * 100` to avoid stale `used_percentage` values carried over from the previous session.
- **Redis integration** — `REDIS_URL` env var (format: `redis://:PASSWORD@host:port/db`). Used by token monitor, file read cache, and warning edge-triggers. All features degrade gracefully when Redis is unavailable. Uses DB0 on the shared agenticore Redis instance. Keys: `agenticore:tokens:{sid}`, `agenticore:token_warn:{sid}`, `agenticore:file_cache:{sid}`, `agenticore:file_mtime:{sid}`.
- **`ENABLE_TOOL_SEARCH=true`** — set in `settings.base.json` `env` block. Makes all MCP tools lazy-loaded on demand, eliminating approximately 79K token upfront cost from MCP tool schemas. Tools appear as "(loaded on-demand)" in `/context`.
- **`agentienv` shell function** (replaces alias) — `agentihooks --loadenv` now installs a proper shell function instead of an alias. The function: (1) defines `agentienv()` which sources `.env` then all `*.env` files alphabetically from `~/.agentihooks/`; (2) auto-calls `agentienv` so vars load in every new shell automatically.
- **`agentihooks ignore` subcommand** — creates a `.claudeignore` in the current directory covering secrets, build artifacts, binaries, venvs, IDE noise. Supports `--force` to overwrite.

### Changed

- **`BlockAction` stderr fix** — `BlockAction` exceptions now print to **stderr** (not stdout) so Claude Code displays the block reason cleanly.
- **10 hook events (not 11)** — `StatusLine` is not a hook event. Valid hook events: `SessionStart`, `SessionEnd`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `UserPromptSubmit`, `Notification`, `PreCompact`, `PermissionRequest`.

## [0.3.0] - 2026-03-07

### Changed

- **Purely additive harness** — agentihooks no longer creates standalone `.claude` directories inside profiles. All install operations target `$HOME/.claude` directly.
- **`CLAUDE_CODE_HOME_DIR`** env var support — points at the home-directory root (`.claude` appended automatically). Priority: `CLAUDE_CODE_HOME_DIR` > `AGENTIHOOKS_CLAUDE_HOME` > `~/.claude`.
- **`~/.claude.json`** now also resolves relative to `CLAUDE_CODE_HOME_DIR` when set.

### Removed

- **`scripts/build_profiles.py`** — generated standalone profile `.claude/` directories intended for `CLAUDE_CONFIG_DIR` usage. Replaced by `agentihooks global --profile <name>` which installs directly into `~/.claude`.
- **Generated `profiles/*/.claude/settings.json`** build artifacts — these contained host-specific paths and are no longer produced.

## [0.2.0] - 2026-03-03

### Added

- **Admin profile** (`profiles/admin/`) — minimal guardrails, secrets warn-only mode.

### Removed

- **`scripts/agent_hub.py`** — agent provisioning moved to agenticore (clones agentihub directly, no build step needed).
- **Publishing profile** (`profiles/publishing/`) — migrated to standalone K8s app in agentihub. Provisioned directly by agenticore.

## [0.1.0] - 2026-02-23

### Added

- Hook system processing all 10 Claude Code lifecycle events
- Modular MCP tool server with 26 tools across 8 categories
- Category-based tool filtering via `MCP_CATEGORIES` env var
- Profile composition system with base settings + per-profile overrides
- Build script for generating profile artifacts (`scripts/build_profiles.py`) *(removed in 0.3.0)*
- Integration clients: AWS, Email, SQS, S3, Webhook, Lambda, DynamoDB, PostgreSQL
- Observability: transcript logging, metrics collection, container log tailing (Docker/K8s/ECS)
- Cross-session tool error memory (learn from past failures)
- Persistent agent memory via Redis + JSONL fallback
- Two default profiles: `default` and `coding`
