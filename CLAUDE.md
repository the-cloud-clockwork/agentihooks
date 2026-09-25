# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# The canonical Python is the workspace venv at ~/dev/tcc-ecosystem/.venv
# (~/.agentihooks/.venv must NEVER exist — the installer no longer looks there).
# Call the venv's binaries directly. It is shared with the other editable repos,
# so never `uv run --active` / `uv sync --active` against it (venv_guard blocks it):
# that syncs it to this repo's lock and moves the other repos' packages.
V=~/dev/tcc-ecosystem/.venv/bin

uv pip install --python $V/python -e ".[all]"                  # install/update deps
$V/python -m pytest                                            # run all tests
$V/python -m pytest tests/test_hook_manager.py                 # single file
$V/python -m pytest tests/test_config.py::TestSecretsMode -v   # single test
$V/ruff check .                                                # lint
$V/ruff format .                                               # format
agentihooks init --profile anton                                       # global install
```

`ruff check` and `ruff format --check` fail independently — CI runs both, so run
both. CI also runs the whole suite, not `-m unit`, which collects under half of it.

## Release & snapshot ritual

The CI manifesto covers the merge method into `main`. These three are specific to
this repo and each one has already cost a broken snapshot:

1. **Release before cutting the snapshot PR.** `release.yml` bumps the version on
   `dev`, so a PR cut first leaves `main` declaring the previous version while
   shipping the new code.
2. **After a squash-merge into `main`, merge `origin/main` back into `dev`
   immediately.** The squash gives the two branches identical trees but no shared
   recent history, so the *next* snapshot PR diffs from the previous merge base and
   replays every file as a phantom conflict. The merge-back changes history only —
   verify with `git rev-parse HEAD^{tree}` before and after, which must match.
3. **`git fetch` updates `origin/dev`, not your local branch.** Releases land on
   `dev` via CI, so a local branch that looks current is usually behind. Merging
   from a stale local `dev` silently reverts whatever CI committed.

## The Four Pillars

AgentiHooks is organized around four pillars. When working on this codebase, understand which pillar a change affects:

| Pillar | Core files | What it does |
|--------|-----------|-------------|
| **Identity** | `scripts/install.py`, `profiles/`, `settings.base.json`, `scripts/targets/` | Profile system, chaining, two-axis model, bundle merge, install targets |
| **Guardrails** | `hooks/secrets.py`, `hooks/context/retry_breaker.py`, `hooks/context/branch_guard.py`, `hooks/context/prod_lockdown.py`, `hooks/context/ci_manifesto.py`, `hooks/context/dep_banner.py`, `hooks/context/_strip.py`, `hooks/context/version_guard.py`, `hooks/context/claude_md_sanity.py` | Two-tier secrets, retry breaker, branch/PR gating, prod lockdown, CI manifesto signal parsing, dep install banner, shared command stripping, version guard, CLAUDE.md bloat guard |
| **Context Intelligence** | `hooks/context/preprocessor.py`, `hooks/context/brain_adapter.py`, `hooks/context/rules_refresh.py`, `hooks/tool_memory.py`, `hooks/context/conditions.py`, `hooks/context/tool_matcher.py` | Token compression, brain injection, one-shot rule refresh to running sessions, tool memory, bundle conditions and the tool matcher they share with enforcements |
| **Fleet Command** | `hooks/context/broadcast.py`, `hooks/mcp/channels.py`, broadcast sections in `hook_manager.py`, CLI in `install.py`, `scripts/claude_quota_balancer.py`, `scripts/claude_terminal.py`, `hooks/context/account_sessions.py`, `hooks/context/quota_policy.py` | Real-time messaging with channel-based targeting, brain adapter, Claude account load balancing and the quota handoff policy |

## Architecture

### Install targets

Three agent CLIs — `claude` (`~/.claude`), `codex` (`~/.codex`), `copilot`
(`~/.copilot`). `SUPPORTED_TARGETS` in `scripts/targets/__init__.py`; each
implements the `TargetAdapter` protocol in `scripts/targets/<name>_target.py`.
`scripts/install.py` is target-agnostic — profile resolution, settings merging
and MCP dict assembly stay there; anything touching a target-specific path or
schema goes through the adapter.

At hook runtime `hooks/targets/` decides how to talk back to whichever CLI
invoked the hook (`AGENTIHOOKS_TARGET`, set by the codex and copilot wrappers;
claude is the default). Branch on a **capability**, not a target name:
`buffers_single_envelope()`, `can_inject_context()`,
`allowed_permission_decisions()`, `supports_arg_mutation()`. An `is_codex()` /
`is_copilot()` check is only correct when the thing really is that CLI's format.

Adding a fourth target means one adapter plus, if its hook I/O differs from
Claude's, entries in `hooks/targets/`. `profiles/`, `hooks/mcp/` and the bundle
repo need no changes — if they do, the seam is broken.

Per-target reference: `docs/reference/CODEX-COMPAT.md`,
`docs/reference/COPILOT-COMPAT.md`.

### Entry points

- **`agentihooks` CLI** → `scripts/install.py:main()` — installs hooks/settings/MCPs, manages profiles/bundles, broadcast CLI
- **Lifecycle hooks** → all 10 hook events point to `python -m hooks` → `hooks/hook_manager.py:main()` (codex wires 10, copilot 12 — the extras fold onto the same handlers)
- **StatusLine** → `hooks/statusline.py` — 2-3 line status bar (not a hook event)
- **MCP tools** → `hooks/mcp/` — separate process registered as `agentihooks`

### Hook dispatch

`hook_manager.py:main()` reads JSON from stdin, routes to `EVENT_HANDLERS[event_name](payload)`:

| Event | Handler | Key behavior |
|---|---|---|
| `SessionStart` | `on_session_start` | Register broadcast session, inject context, brain injection, MCP warning |
| `SessionEnd` | `on_session_end` | Deregister session, clear caches, log summary |
| `UserPromptSubmit` | `on_user_prompt_submit` | Secrets scan, CI-manifesto refresh, amygdala check, channel-filtered broadcast delivery |
| `PreToolUse` | `on_pre_tool_use` | Secrets scan, guardrails pipeline, brain refresh cadence, enforcement drumbeat, critical broadcast via additionalContext |
| `PostToolUse` | `on_post_tool_use` | Bash filter, file dedup, tool error recording |
| `Stop` / `SubagentStop` | `on_stop` | Memory auto-save, cost logging |

`BlockAction` is caught at `main()` top → stderr + exit 2 → Claude Code reads the block reason.

### Configuration

`hooks/config.py` auto-loads `~/.agentihooks/.env` + `~/.agentihooks/*.env` at import time. All feature flags are env vars.

### Settings installation

`scripts/install.py` reads the target's native base (`_base/settings.base.json` for claude, `config.base.toml` for codex, `settings.base.copilot.json` for copilot), substitutes `__PYTHON__`/`/app` placeholders, deep-merges the bundle-global, profile-chain and settings-profile layers found under that target's subdir (`.claude/`, `.codex/`, `.copilot/`), and hands the result to `adapter.write_settings(native)`. Each adapter receives a document already in its own format — nothing is translated between targets. `_native_layer_path()` and `_load_native_layer()` own discovery and JSON/TOML loading.

### Profile system

3-layer merge: agentihooks built-in → bundle global → profile-specific. Profiles chained with commas. Two-axis model: persona (rules/CLAUDE.md) independent from settings (permissions/MCP).

### Broadcast system + channels

File-based pub/sub at `~/.agentihooks/broadcast.json`. Sessions auto-register/deregister via hooks. Three severity tiers (info/alert/critical). AI-assisted `emit` spawns sandboxed Haiku (Bash(agentihooks*) only).

**Channels:** Messages can have an optional `channel` field. Subscriptions are env-driven via `AGENTIHOOKS_BASE_CHANNELS` (comma-separated). Default ships in `profiles/default/.claude/settings.overrides.json` `env` block as `"brain,amygdala"`. Layering: profile env → repo `.claude/settings.json` → repo `.claude/settings.local.json` → container ENV at launch (highest). Empty / unset → session only receives global broadcasts (messages with no `channel` field).

### Brain adapter

`hooks/context/brain_adapter.py` bridges brain-api `/feed` or the legacy file source to the broadcast channel system. SessionStart always reconciles the feed; PreToolUse refreshes it every `BRAIN_REFRESH_TOOL_CALLS` tool calls (default 20) and injects new or restored entries into that tool call. Hot arcs default to 10 and each entry remains capped by `BRAIN_PAYLOAD_MAX_BYTES` (default 1536). Source failures preserve the last-known-good channel.

### Claude account load balancing

Each subscription is an `AH_CC_TOKEN_<slug>`; a routed session keeps exactly one,
so the variable **name** identifies its account (never read the value out).
`agenti` (`cmd_claude`) picks the most routing left among accounts below
`AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT` live sessions, counted from `/proc` by
`hooks/context/account_sessions.py`. `hooks/context/quota_policy.py` is pure code
deciding HANDOFF / WAIT / STOP / PUSH from the session's statusline quota and the
router cache; PreToolUse blocks on STOP and WAIT. `claude-terminal --handoff` runs
the handoff and marks the old session `handed_off`. Register the real agent PID
(`agent_pid()`), never `os.getppid()` — that is the hook's short-lived shell.
Docs: `docs/pillars/load-balancing.md`.

### Testing patterns

Tests mock Redis via `patch("hooks._redis.get_redis", return_value=None)`. Use `uv run` for all test/lint commands.

`tests/conftest.py::_isolate_real_user_paths` is autouse and refuses to run if
any target's config home still resolves under the real `$HOME` — add a new
target's resolver there or its tests will write into the operator's live
install. Live-CLI smoke tests (`scripts/codex_smoke.sh`,
`scripts/copilot_smoke.sh`) are not part of the pytest suite; they need the
respective CLI installed and authenticated, and assert only hook-attributable,
session-scoped evidence.
