# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Controls toggle (bypass mode) — Guardrail 9** — operator phrase `disable controls` (also `turn off controls`, `deactivate controls`, `kill controls`) flips a session-wide bypass that short-circuits CI-manifesto signal gates: branch creation (§13), PR creation (§14, including the 3-PR session counter), `gh pr merge` to main (§4 release), `gh workflow run release.yml`, `:latest`/`:prod`/`:stable` image push (§5 hotfix), and force push to non-main branches. Spawned subagents inherit the unlock automatically via a single global flag (`~/.agentihooks/controls_flags/active.flag` + Redis key `controls_disabled:_global`). Restored by `enable controls` (also `turn on`, `activate`, `restore`) or by SessionEnd of the activating session. HARD FLOOR (push-to-main, force-push to main, commit-on-main, `--base main` PR requirement, `git tag`, `git reset main`, `git branch -D main`, secrets-in-files) stays enforced. New module `hooks/context/controls_toggle.py`, integrations in `branch_guard._has_branch_signal` / `_has_pr_signal` / PR-counter / force-push loop / `prod_lockdown.check_prod_lockdown`. Banner injected on every transition and on each turn while active. Feature flag: `CONTROLS_BYPASS_ENABLED` (default true).
- **`agentihooks refresh-rules` CLI** — one-shot push of profile rule updates into running Claude sessions without restart. Writes `~/.agentihooks/force_refresh/rules-<profile>.json` with the current rule payload (`CLAUDE.md` + `rules/*.md` + `CLAUDE.local.md`) and a snapshot of alive session IDs. On each session's next `UserPromptSubmit`, the hook injects the payload if that session is in pending, then removes it from the list. Sessions started AFTER the push never see the marker. Markers auto-GC after 24h. Flags: `--profile`, `--dry-run`, `--clear`.
- **`hooks/context/dep_banner.py`** — PreToolUse hook that emits a visible banner when Bash runs a dependency install (pip, npm, cargo, uv, poetry, pipx, yarn, pnpm, go, gem, apt, brew, pacman, dnf, yum, apk). Never blocks — surfaces every third-party code addition for supply chain audit.
- **`hooks/context/rules_refresh.py`** — module backing `refresh-rules`. Public API: `write_refresh_marker`, `maybe_inject`, `gc_all_expired`, `collect_profile_rules`.
- **`hooks/context/_strip.py`** — shared command-stripping utility. Removes heredoc bodies (any delimiter), echo/printf/curl/python-c/jq/awk/sed quoted arguments before guards apply regex. Prevents false-positive blocks on documentation text in command payloads.
- **Two-tier secrets handling** — Write/Edit/Bash-with-file-redirect containing a secret still hard-blocks. Inline Bash secrets (no file write) scan + log + NOTE only. Transcript secrecy is operator-managed.
- **Session-scoped signal persistence** — PR creation, release gate (`gh pr merge`, `release.yml`), and hotfix signals now persist for the full session. Branch creation and `--emergency-prod` stay per-turn. PR signal has a 3-per-session counter; re-signal resets it. `gh pr create` enforces `--base main`.
- **Subagent signal isolation** — subagents cannot self-arm release/hotfix/PR signals via their own prompt text. Only top-level operator sessions can arm prod-impacting signals.
- **Session supersede on re-register** — when a new `session_id` registers from a PID that already has an alive session, the previous entry is marked `status="superseded"` (kept 24h, not deleted). Fixes the "alive session flood" where one long-running Claude process accumulated 35 stale entries from `/resume` / `/clear` cycles.
- **`sessions list` UX** — new NAME column reading `custom-title` / `agent-name` events from JSONL (set by Claude Code `/rename` or `--name` per April 2026 release). `register_session` preserves `started_at` across re-registrations so AGE reflects true session lifetime. Sort ranks alive above closed/dead/superseded.
- **Negation-aware signal matching** — signal matchers skip matches preceded by `don't`, `not`, `never`, `shouldn't`, `won't`, `can't`. Prevents "don't merge to main" from arming the release gate.
- **Per-project profile override** — `.agentihooks.json` `profile` field controls which profile generates `settings.local.json` and `CLAUDE.local.md` per project. Supports profile chains.
- **`CLAUDE.local.md` generation** — `agentihooks init --local` generates `.claude/CLAUDE.local.md` from the resolved profile's `CLAUDE.md`. Auto-gitignored.
- **Hierarchy-aware MCP blacklist** — parent projects exclude MCP servers that child projects whitelist via `.agentihooks.json`.
- **Orphaned MCP server pruning** — sync daemon removes stale servers from `~/.claude.json` not defined in any source file.
- **`--query` CWD awareness** — reads `.agentihooks.json` from current directory first, shows `coding (local)` vs `anton (global)`.
- **Daemon restart on init** — always kills and restarts sync daemon to pick up code changes.
- **Per-project docs page** — new `docs/getting-started/per-project.md`.
- **Sync daemon (`agentihooks daemon`)** — background daemon that watches all source files feeding the install pipeline (profiles, `settings.base.json`, connectors, bundles, MCP files, `.env`) and auto-propagates changes to all registered downstream consumers. Uses SHA-256 hashing with category-based change detection. Targets are registered automatically by `agentihooks init` and `agentihooks init --repo`. Configurable poll interval (default 60s, env: `AGENTIHOOKS_SYNC_POLL_SEC`). Advisory file lock prevents concurrent writes. State: PID at `~/.agentihooks/sync-daemon.pid`, hashes at `~/.agentihooks/sync-hashes.json`, log at `~/.agentihooks/logs/sync-daemon.log`.
- **Target registry in `state.json`** — `agentihooks init` and `agentihooks init --repo <path>` now register their targets (path + profile) in `state.json` under a new `targets` key. The sync daemon uses this registry to know what to re-install when source files change.
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

- **`get_active_sessions()` filters to alive-only by default** — previously returned every registry entry (including superseded, closed, dead), so `agentihooks status` reported 40 sessions when only 5 were actually alive. New `include_all=True` param for callers who need the full registry. `cleanup=True` now MARKS dead entries instead of deleting (preserves the 24h retention window).
- **Guards fail-closed on hook exception** — branch_guard and prod_lockdown now emit a stderr warning when an unexpected exception escapes the guard body. The outer `main()` catch now exits 1 on infrastructure errors (previously exited 0, so hook failures were silent).
- **`get_env` MCP tool hardened** — requires a non-empty filter (≥2 chars), refuses to dump the full environment. Redacts values of keys matching `key|secret|token|password|credential|dsn|auth|private|signing`.
- **Retry circuit breaker reinforced** — message at N=5 now includes explicit `Agent(subagent_type="error-researcher", model="haiku")` spec and counter visibility. Hard-block at N=10 rewritten with the same explicit spawn instructions. Stderr signal added for maximum visibility.
- **CI Manifesto doctrine updated** — added §15 Dependency Install Protocol, §16 Secrets Two-Tier, §17 Execute-End-to-End Doctrine. §12 clarifies `dev` is the ONLY auto-created branch without signal. §4/§14 scope language updated to reflect session-scoped signals.
- **Anton profile rules** — `operator-live-deploy.md` inverted: commit-push-CI-Monitor is the primary path, live-patch is a troubleshooting exception. `operator-delegation.md` rewritten: execute-end-to-end is a hard rule, defer log is documentation not a pause trigger. `operator-clearance.md` aligned with hook authority (hooks are enforcement, rules describe intent). New `operator-dependency-protocol.md`.
- **`BlockAction` stderr fix** — `BlockAction` exceptions now print to **stderr** (not stdout) so Claude Code displays the block reason cleanly.
- **10 hook events (not 11)** — `StatusLine` is not a hook event. Valid hook events: `SessionStart`, `SessionEnd`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `UserPromptSubmit`, `Notification`, `PreCompact`, `PermissionRequest`.

### Fixed

- **False-positive guard blocks** — `_strip_safe_content` in `branch_guard` and `prod_lockdown` only stripped heredoc bodies with literal `<<'EOF'` or `<<EOF`. Bash commands containing `echo "gh pr merge to main"`, `curl -d '{"msg":"..."}'`, `python -c "print(...)"`, or heredocs with alternate delimiters (`<<YAML`, `<<-EOF`) triggered false blocks. Now delegates to shared `hooks/context/_strip.py` which handles all these cases.
- **Cross-pipe pattern matches** — `.*` in branch_guard merge/rebase/reset patterns crossed `&&` and `|` boundaries, so multi-command lines referencing `main` in a read subcommand could trip the merge guard. Replaced with `[^|&;\n]*` to respect command separators.
- **Subagent signal leak** — `on_subagent_stop` did not clear subagent signals under `agent_id`, so a subsequent subagent with the same `agent_id` inherited the previous one's signal state for up to 5 minutes. Added signal-clear block mirroring `on_stop`.
- **Container log parameter injection** — kubectl/docker/aws argv parameters in `tail_container_logs` MCP tool were unsanitized. Added `^[a-zA-Z0-9._:/@-]+$` validation and a 200-char cap on `filter_regex` to prevent flag injection and ReDoS.
- **Channel name validation** — `channel_subscribe` and `channel_unsubscribe` MCP tools now validate names against `^[a-zA-Z0-9._-]+$` to prevent config corruption from path-traversal or JSON-special characters.
- **`auto_dev_switch` env stripping** — `_git` helper constructed a minimal env that stripped `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`, credential-helper vars. The `git push -u origin dev` step silently failed on SSH-gated origins. Now inherits `os.environ` and overlays only `GIT_ALLOW_MAIN_PUSH=1`.

## [0.3.0] - 2026-03-07

### Changed

- **Purely additive harness** — agentihooks no longer creates standalone `.claude` directories inside profiles. All install operations target `$HOME/.claude` directly.
- **`CLAUDE_CODE_HOME_DIR`** env var support — points at the home-directory root (`.claude` appended automatically). Priority: `CLAUDE_CODE_HOME_DIR` > `AGENTIHOOKS_CLAUDE_HOME` > `~/.claude`.
- **`~/.claude.json`** now also resolves relative to `CLAUDE_CODE_HOME_DIR` when set.

### Removed

- **`scripts/build_profiles.py`** — generated standalone profile `.claude/` directories intended for `CLAUDE_CONFIG_DIR` usage. Replaced by `agentihooks init --profile <name>` which installs directly into `~/.claude`.
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
