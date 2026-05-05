---
title: CLI Commands
nav_order: 3
parent: Reference
---

# CLI Commands
{: .no_toc }

The `agentihooks` CLI is installed globally via `uv tool install --editable .` as part of `agentihooks init`. All subcommands are idempotent.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## `agentihooks init`

The single entry point for installing agentihooks. Handles global setup, bundle linking, and per-repo configuration.

```bash
agentihooks init [--bundle <path>] [--profile <name>] [--repo <path>]
```

### What it does

1. Links bundle directory (if `--bundle` is provided)
2. Merges settings: `_base/settings.base.json` -> profile `.claude/settings.overrides.json` -> OTEL
3. Substitutes `/app` -> real repo path and `__PYTHON__` -> venv Python in all commands
4. Preserves personal keys (`model`, `autoUpdatesChannel`, `skipDangerousModePermissionPrompt`) from any pre-existing unmanaged settings
5. Writes `~/.claude/settings.json` with hook wiring and tool permissions
6. Symlinks skills, agents, commands, and rules via 3-layer merge (agentihooks built-in -> bundle global -> each profile in chain)
7. Writes `~/.claude/CLAUDE.md` -- single profile: file copy; chained profiles: concatenated with `---` separators and `<!-- profile: name -->` markers
8. Installs MCPs (hooks-utils + bundle `.claude/.mcp.json` + profile `.claude/.mcp.json`)
9. Applies MCP blacklist across all projects (`disabledMcpServers`)
10. Installs the `agentihooks` CLI globally via `uv tool install --editable .`
11. Restarts sync daemon
12. Writes managed bashrc block (`agentienv` function + `agenti` alias)

### Flags

| Flag | Description |
|------|-------------|
| `--bundle <path>` | Path to bundle directory. First-time: links the bundle and runs global install. |
| `--profile <name>` | Profile to install. Comma-separated for chaining: `--profile coding,anton` (default: `default`, env: `AGENTIHOOKS_PROFILE`) |
| `--repo <path>` | Target repo directory for per-repo configuration |
| `--force` | Clean install — resets install state (`state.json`, sync hashes, session caches, PID files, `prod_bypass/`, `controls_flags/`, `voice_flags/`, `force_refresh/`) and re-symlinks `~/.claude/` assets. **Preserves** broadcasts, enforcements, brain data, logs, quota accounts, `.env`, `.venv`, memory mirror. |

### Environment variables

| Variable | Description |
|----------|-------------|
| `AGENTIHOOKS_PROFILE` | Default profile when `--profile` is not passed (default: `default`) |
| `AGENTIHOOKS_SETTINGS_PROFILE` | Default settings-only overlay profile (default: none) |
| `AGENTIHOOKS_MCP_FILE` | Path to an MCP JSON file to auto-merge into `~/.claude.json` during install |
| `CLAUDE_CODE_HOME_DIR` | Home-directory root override -- `.claude` is appended automatically (default: `$HOME`) |
| `AGENTIHOOKS_CLAUDE_HOME` | Legacy: direct path to the `.claude` directory (default: `~/.claude`) |
| `AGENTIHOOKS_HOME` | Override the agentihooks state directory (default: `~/.agentihooks`). Used for per-pod isolation on shared filesystems — set to `/shared/.agentihooks-<pod-name>` so each pod gets its own state without racing on `state.json`. |

### Examples

```bash
# First-time install with bundle
agentihooks init --bundle ~/dev/my-tools --profile coding

# Re-run global install (uses linked bundle)
agentihooks init

# Install with a different profile
agentihooks init --profile admin

# Clean install (fresh state, preserves .env)
agentihooks init --force --profile coding

# Install with persona + settings overlay
agentihooks init --profile anton --settings-profile admin

# Quick-switch settings layer only (keeps persona intact)
agentihooks settings-profile admin

# Revert settings to persona defaults
agentihooks settings-profile --clear

# Same, using the environment variable
AGENTIHOOKS_PROFILE=coding agentihooks init

# Per-repo configuration
agentihooks init --repo ~/dev/my-project

# Auto-merge a gateway MCP file during install
AGENTIHOOKS_MCP_FILE=/shared/gateway-mcp.json agentihooks init
```

---

## `agentihooks settings-profile`

Quick-switch the settings layer without touching persona (rules, CLAUDE.md, skills, agents, commands).

```
agentihooks settings-profile [NAME] [--clear]
```

| Argument / Flag | Description |
|----------------|-------------|
| `NAME` | Settings profile to apply. Only its `settings.overrides.json` and `.mcp.json` are used. |
| `--clear` | Remove the settings overlay and revert to persona profile defaults. |

With no arguments, shows the current persona and settings profile.

### Environment variable

```bash
export AGENTIHOOKS_SETTINGS_PROFILE=admin
agentihooks init --profile anton   # automatically uses admin settings overlay
```

### Examples

```bash
# Show current state
agentihooks settings-profile

# Switch to admin settings (keeps anton persona)
agentihooks settings-profile admin

# Revert to persona defaults
agentihooks settings-profile --clear
```

---

## `agentihooks broadcast`

Send a message to all active Claude Code sessions simultaneously.

```
agentihooks broadcast [OPTIONS] MESSAGE
```

| Flag | Default | Description |
|------|---------|-------------|
| `-s`, `--severity` | `alert` | `critical`, `alert`, or `info` |
| `-t`, `--ttl` | per severity | Time-to-live: `5m`, `30m`, `1h`, `8h`, `24h` |
| `--persistent` | per severity | Re-inject on every hook event until TTL expires |
| `--source` | `operator` | Source tag: `operator`, `system`, `cron`, `api` |
| `--list` | | Show all active broadcasts |
| `--clear [ID]` | | Clear all broadcasts, or a specific one by ID |

### Severity behavior

| Severity | Injection | Default TTL | Persistent |
|----------|-----------|-------------|------------|
| `critical` | Every turn + every tool call | 30 min | Yes |
| `alert` | Every turn | 1 hour | Yes |
| `info` | Once per session | 4 hours | No |

### `agentihooks broadcast emit`

AI-assisted broadcast composition. Describe the message in natural language and Haiku selects the appropriate severity, TTL, and wording.

```
agentihooks broadcast emit NATURAL_LANGUAGE_DESCRIPTION
```

The subcommand sends the description to Claude Haiku, which returns a structured broadcast (severity, TTL, message text) and immediately posts it.

```bash
# Haiku picks severity=critical, TTL=30m
agentihooks broadcast emit "prod API is returning 500s, stop all deploys immediately"

# Haiku picks severity=alert, TTL=8h
agentihooks broadcast emit "deploy freeze tonight until the on-call engineer clears it"

# Haiku picks severity=info, TTL=4h
agentihooks broadcast emit "sonarqube is down for maintenance"
```

### Examples

```bash
# Emergency (manual)
agentihooks broadcast -s critical "Production incident — do NOT deploy"

# Deploy freeze (manual)
agentihooks broadcast -s alert -t 8h "Deploy freeze until 6am"

# Info (manual)
agentihooks broadcast -s info "SonarQube is down"

# AI-assisted emit
agentihooks broadcast emit "prod database is read-only until the migration completes"

# List / clear
agentihooks broadcast --list
agentihooks broadcast --clear
```

---

## `agentihooks refresh-rules`

Push profile rule updates into every running Claude Code session without a restart. Each target session consumes the refresh once on its next `UserPromptSubmit`.

```bash
agentihooks refresh-rules [--profile <name>] [--dry-run] [--clear]
```

### How it works

1. Reads the installed rules: `~/.claude/CLAUDE.md` and every `~/.claude/rules/*.md`.
2. Takes a snapshot of currently-alive session IDs from the broadcast registry.
3. Writes `~/.agentihooks/force_refresh/rules-<profile>.json` containing the payload + pending session list.
4. On each targeted session's next `UserPromptSubmit`, the hook injects the payload and removes the session from pending.
5. When pending drains → marker deleted. Otherwise marker auto-GCs after 24h.

Sessions started AFTER the push never see the marker — they get fresh rules at `SessionStart`, so re-injection would be redundant.

### Flags

| Flag | Description |
|------|-------------|
| `--profile <name>` | Profile name (default: detected from the `~/.claude/CLAUDE.md` symlink target) |
| `--dry-run` | Print what would be pushed (profile, content hash, payload size, target session IDs) without writing the marker |
| `--clear` | Delete any existing pending marker for the profile (cancel a push in progress) |

### Examples

```bash
# Preview which sessions would be hit
agentihooks refresh-rules --dry-run

# Push the current rules to all alive sessions
agentihooks refresh-rules

# Cancel a pending marker without waiting for TTL
agentihooks refresh-rules --clear
```

---

## `agentihooks sessions`

Crash-recovery session picker. Lists recent Claude Code sessions (24h window) with names, lifetimes, and IDs. Reopen a session by index from the list.

![agentihooks sessions output showing alive, closed, and superseded sessions with NAME and AGE columns](/agentihooks/assets/sessions-list-with-names.png)

```bash
agentihooks sessions list [--hours N] [--limit N]
agentihooks sessions reopen <IDX> [--force]
agentihooks sessions backfill [--hours N]
```

### Columns

| Column | Meaning |
|--------|---------|
| `IDX` | Index to pass to `reopen` |
| `STATUS` | `alive` / `closed` / `dead` / `superseded` |
| `AGE` | For `alive`: session lifetime (time since `started_at`). For others: time since last activity. |
| `NAME` | Session title from Claude Code `/rename` or `--name` flag, or first user message snippet |
| `CWD` | Working directory (home-relative, truncated if long) |
| `ID` | Session UUID |

### Subcommands

- **`list`** (alias `ls`) — show recent sessions. Default: 10 most recent in the last 24h. `--hours` controls the lookback window; `--limit 0` shows all.
- **`reopen <IDX>`** (alias `open`) — relaunch Claude Code resuming the selected session. Uses Windows Terminal on WSL when available.
- **`backfill`** — seed the registry from `~/.claude/projects/*.jsonl` for sessions that started before agentihooks was installed.
- **`reconcile`** — health-check the registry.

### Sort behavior

Alive sessions appear first (longest-running on top), followed by closed, dead, and superseded. Supersede is used for session IDs that were cycled by `/resume` or `/clear` within the same PID — they're kept for audit but can't be reopened.

---

## `agentihooks uninstall`

Remove everything agentihooks installed from the system.

```bash
agentihooks uninstall [--yes]
```

### What gets removed

- `~/.claude/settings.json` -- if managed by agentihooks (detected via `_managedBy` marker)
- Skills, agents, commands, and rules symlinks in `~/.claude/` -- if they target the agentihooks repo
- `~/.claude/CLAUDE.md` -- if it points into `profiles/`
- MCP servers in `~/.claude.json` -- from profile `.mcp.json` files and `state.json`
- Running daemons -- sync daemon is stopped
- Bashrc block -- the `agentienv` function and `agenti` alias are removed from `~/.bashrc`
- `agentihooks` CLI -- via `uv tool uninstall agentihooks`

### What is NOT removed

`~/.agentihooks/` (user data: logs, memory, state.json) is left in place. To fully reset:

```bash
rm -rf ~/.agentihooks
```

### Flags

| Flag | Description |
|------|-------------|
| `--yes` | Skip confirmation prompt (for scripting) |

---

## `agentihooks claude`

Launch Claude Code with flags derived from the active profile's `profile.yml`.

```bash
agentihooks claude [extra-args...]
```

**Alias:** `agenti` (installed by `agentihooks init` in the bashrc block)

### How it works

Reads the `claude:` section from the active profile's `profile.yml` and maps fields to Claude Code CLI flags:

| profile.yml field | CLI flag |
|-------------------|----------|
| `claude.model` | `--model <value>` |
| `claude.max_turns` | `--max-turns <value>` |
| `claude.permission_mode: bypassPermissions` | `--dangerously-skip-permissions` |

Any extra arguments are passed through to Claude Code.

### Examples

```bash
# Launch with profile settings
agentihooks claude

# Use the alias
agenti

# Pass extra args to claude
agenti --verbose
```

---

## `agentihooks daemon`

Manage the sync daemon that watches asset directories (skills, agents, commands, rules, MCP servers, `.env` files) and auto-propagates changes to all registered downstream consumers.

```bash
agentihooks daemon [action]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `start` *(default)* | Start the background daemon. Auto-detaches, writes PID to `~/.agentihooks/sync-daemon.pid`, logs to `~/.agentihooks/logs/sync-daemon.log`. |
| `stop` | Kill the running daemon using the PID file. |
| `status` | Show daemon PID, registered targets, watched file count, and last scan timestamp. Flags `[PATH MISSING]` for project paths that no longer exist. |
| `logs` | Runs `tail -f` on `~/.agentihooks/logs/sync-daemon.log`. |

### How it works

The sync daemon uses manifest hashing to detect changes. On each poll cycle:

1. Hashes every source file (profiles, settings, bundles, MCP files, `.env` files) using SHA-256
2. Compares against previous hashes
3. If changes are detected, re-runs the install pipeline for affected targets
4. Propagation is **additive only** -- new skills/agents/commands/rules are symlinked automatically

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--poll N` | `60` | Poll interval in seconds. Also configurable via `AGENTIHOOKS_SYNC_POLL_SEC` env var. |
| `--foreground` | -- | Run in foreground instead of daemonizing. Useful for debugging. |

### Target registration

Targets are registered automatically:
- `agentihooks init` registers `~/.claude/` as the global target with the chosen profile.
- `agentihooks init --repo <path>` registers the project path with the chosen profile.

Registered targets are stored in `~/.agentihooks/state.json` under the `targets` key.

### Propagation rules

| Source change | Affected targets |
|---|---|
| `settings.base.json` | Global + all projects + MCP sync |
| Profile files (`profile.yml`, `settings.overrides.json`, `CLAUDE.md`) | Targets using that profile |
| MCP files | MCP sync only (`~/.claude.json`) |
| `.env` files in `~/.agentihooks/` | Global + all projects |
| Bundle directory contents | Global + all projects |
| Skills, agents, commands, rules | Re-symlinked via 3-layer merge |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIHOOKS_SYNC_POLL_SEC` | `60` | Daemon poll interval in seconds. |

---

## `agentihooks memory-sync`

Cross-machine auto-memory sync with PR-gated fleet propagation. Mirrors
**only** `~/.claude/projects/*/memory/` to a private git remote via
[gitfoam](https://github.com/The-Cloud-Clockwork/gitfoam); each machine
pushes to its own `gitfoam/<hostname>/main` branch, consumers merge from
`origin/main` only, and promotion is a GitHub PR.

```bash
agentihooks memory-sync <action> [--purge] [--auto-merge] [--idle-days N]
```

### Actions

| Action | Description |
|--------|-------------|
| `install` | Build/verify `gitfoam`, **seed `origin/main`** from local memory, init mirror, start daemon |
| `start` | Start the gitfoam watch daemon on the mirror directory |
| `stop` | Stop the gitfoam daemon |
| `status` *(default)* | Show mode, config, binary path, and daemon PID |
| `sync-now` | Run one `tick()` manually (rsync in + git fetch main + merge) |
| `propose` | Open a PR from `gitfoam/<hostname>/main` → `main` via `gh pr create`. `--auto-merge` arms `gh pr merge --auto --squash`. |
| `sweep-branches` | Delete remote branches already merged to main + idle > `MEMORY_MIRROR_SWEEP_IDLE_DAYS` |
| `migrate-layout` | One-off: rewrite `origin/main` to the v3 `by-project/<key>/` layout from the machine's current identity map. Dry-run by default; `--confirm` executes. Also deletes all `gitfoam/*` and `proposal/*` branches to clear the old layout. |
| `uninstall` | Stop daemon (add `--purge` to also remove the mirror directory) |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_MIRROR_ROLE` | `off` | v4 node role. `off` / `consumer` / `offline` / `contributor` / `authority`. Preferred over `MODE`. |
| `MEMORY_MIRROR_MODE` | `off` | v3 legacy. `off` / `write` / `write-local-only`. Derived to `off` / `contributor` / `offline` when `ROLE` unset. Legacy `MEMORY_MIRROR_ENABLED=true` maps to `contributor`. |
| `MEMORY_MIRROR_REMOTE` | *(unset)* | Git URL of the private mirror repo. **Required.** |
| `MEMORY_MIRROR_DIR` | `~/.agentihooks/memory-mirror` | Local mirror directory (owned by agentihooks). |
| `MEMORY_MIRROR_BRANCH_PREFIX` | `gitfoam` | Branch namespace; machine pushes to `<prefix>/<hostname>/main`. |
| `MEMORY_MIRROR_INTERVAL_SEC` | `60` | Pull tick interval (push is always ~500ms via gitfoam). |
| `MEMORY_MIRROR_SWEEP_IDLE_DAYS` | `15` | Minimum idle days before a merged branch is swept. |
| `MEMORY_MIRROR_CLAUDE_PROJECTS` | `~/.claude/projects` | Source tree. Override for testing only. |
| `GITFOAM_BINARY` | `~/.cargo/bin/gitfoam` | Path to the gitfoam binary. |
| `GITFOAM_LOCAL_SOURCE` | *(unset)* | Local gitfoam checkout; if set and binary missing, install runs `cargo install --path`. |

### Roles (v4)

Each node's behavior in the tick loop is determined by `MEMORY_MIRROR_ROLE`:

- `off` — dormant (default)
- `consumer` — fetch + consume main only; no snapshot, no gitfoam push (read-only pods)
- `offline` — snapshot + gitfoam push only; no fetch/merge (air-gapped contributors)
- `contributor` — snapshot + push + fetch + merge; promote to main via `propose` (v3 default)
- `authority` — contributor pipeline + direct `git push --force-with-lease` to `origin/main`. EXACTLY ONE per fleet.

Consumers install without gitfoam — `agentihooks memory-sync install` skips
the Rust build/daemon entirely when `ROLE=consumer`. `propose` and
`sweep-branches` error out on non-contributor roles.

Legacy `MEMORY_MIRROR_MODE` (v3): `write`→`contributor`, `write-local-only`→`offline`.

### Scope — memory only

The snapshot rsync sources only `<encoded>/memory/` per project, so transcripts
(`*.jsonl`), `ctx_refresh_*.json`, `todos/`, and `tool-results/` are never even
visible to the mirror. Each project's memory lands at
`by-project/<identity-key>/memory/` (or `_unmapped/<encoded>/memory/` for
unresolvable paths).

### Identity resolution (v3)

For each `~/.claude/projects/<encoded>/` dir, the resolver:

1. **Decodes** the encoded name back to a real filesystem path by greedy-walking
   (handles hyphenated directory names like `tccw-toolbelt` correctly).
2. **Walks up** from that path looking for a package/agent boundary marker in
   this priority order:
   - `agent.yml` (fleet agent boundary — highest)
   - `pyproject.toml` / `Cargo.toml` / `package.json` / `go.mod`
   - `.git/` (repo root — fallback)
3. **Uses `basename()`** of the boundary dir as the identity key.

If decoding or boundary detection fails, the project lands in `_unmapped/`.

### Conflict model

On divergent edits between local and `origin/main`, the merge step writes the
incoming version to a sibling `<name>.conflict-<hostname>-<epoch><ext>` — the
local file is never overwritten. Resolve via `/memory`, then delete the
conflict file.

### Examples

```bash
# First-time setup (after creating the private repo and setting env vars)
gh repo create <org>/claude-memory-mirror --private --confirm
agentihooks memory-sync install

# Promote today's learnings to main
agentihooks memory-sync propose
agentihooks memory-sync propose --auto-merge     # auto-merge when PR is clean

# Force a tick
agentihooks memory-sync sync-now

# Housekeeping (safe on cron)
agentihooks memory-sync sweep-branches
agentihooks memory-sync sweep-branches --idle-days 30

# v3 one-off: migrate a v1/v2 mirror to the by-project/<key>/ layout
agentihooks memory-sync migrate-layout              # dry-run (prints plan)
agentihooks memory-sync migrate-layout --confirm    # execute

# Full rollback
agentihooks memory-sync uninstall --purge
```

See also: [Memory Mirror guide](../getting-started/memory-mirror.md).

---

## `agentihooks ignore`

Create a `.claudeignore` in the current working directory (or a given path). Claude Code uses `.claudeignore` to exclude files from reading and indexing -- keeping credentials, build artefacts, and binaries out of the context window.

```bash
agentihooks ignore [path] [--force]
```

### What it creates

A `.claudeignore` covering:

| Section | Examples |
|---------|---------|
| Credentials & secrets | `.env`, `.env.*`, `*.pem`, `*.key`, `secrets/` |
| Build artefacts | `__pycache__/`, `dist/`, `node_modules/`, `target/`, `*.egg-info/` |
| Runtime data | `*.log`, `*.sqlite`, `*.db`, `*.lock` |
| Test output | `.coverage`, `htmlcov/`, `junit*.xml` |
| IDE / OS noise | `.idea/`, `.vscode/`, `.DS_Store`, `Thumbs.db` |
| Large binaries / media | archives, images, video, fonts |
| Virtual environments | `.venv/`, `venv/`, `env/` |
| IaC state | `.terraform/`, `*.tfstate`, `.terraform.lock.hcl` |

`.env.example` is explicitly un-ignored (`!.env.example`) so the template remains visible.

### Flags

| Flag | Description |
|------|-------------|
| `path` | Target directory (default: current directory) |
| `--force` | Overwrite an existing `.claudeignore` |

### Examples

```bash
# Create in current directory
agentihooks ignore

# Create in a specific project
agentihooks ignore ~/dev/my-project

# Overwrite an existing file with a fresh template
agentihooks ignore --force
```

---

## `agentihooks --list-profiles`

Print all available profiles and exit. Shows profiles from both the agentihooks repo and any linked bundle.

```bash
agentihooks --list-profiles
```

---

## `agentihooks bundle`

Manage the linked bundle directory.

```bash
agentihooks bundle <action> [path] [--rebase]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `link <path>` | Link a bundle directory. Stores the path in `state.json`. |
| `unlink` | Unlink the current bundle. |
| `list` | Show the linked bundle path, linked date, and available profiles. |
| `pull` | Run `git pull` on the linked bundle directory. |
| `pull --rebase` | Run `git pull --rebase` on the linked bundle directory. |

### Examples

```bash
# Link a bundle
agentihooks bundle link ~/dev/my-tools

# Update bundle from remote
agentihooks bundle pull

# Update with rebase
agentihooks bundle pull --rebase

# Show bundle info
agentihooks bundle list

# Unlink
agentihooks bundle unlink
```

---

## `agentihooks link-profile`

Link an external directory as a chain-able profile. Where `bundle link` registers a *collection* of profiles, `link-profile` registers a *single* profile dir at any path on disk and (by default) appends it to the active chain.

```bash
agentihooks link-profile link <path> [--name <alias>] [--no-append] [--no-init]
agentihooks link-profile unlink <name> [--no-init]
agentihooks link-profile list
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `link <path>` | Register `<path>` as a linked profile. Default behavior: append the basename to the active global chain and re-run `agentihooks init` so settings, CLAUDE.md, rules, and MCP all reflect the new chain. |
| `unlink <name>` | Remove the linked profile from the registry, strip it from the active chain, sweep any symlinks pointing into it, and re-install. |
| `list` | Show all linked profiles, their paths, the link date, and which are currently in the chain. Flags missing paths as `[MISSING]`. |

### Flags

| Flag | Effect |
|------|--------|
| `--name <alias>` | Use `<alias>` instead of the directory basename. Required if the basename collides with a built-in or bundle profile (link will refuse otherwise). |
| `--no-append` | Register the path in `state.linked_profiles` but do not modify the active chain. |
| `--no-init` | Update state but skip the immediate re-install. Operator runs `agentihooks init` later. |

### Examples

```bash
# Link an external profile dir → chain becomes anton,brain → install reapplied
agentihooks link-profile link ~/dev/brain-profile

# Link with explicit alias (avoids collision with built-in)
agentihooks link-profile link ~/dev/anton-fork --name anton2

# Register without touching the chain or running install
agentihooks link-profile link ~/dev/brain --no-append --no-init

# Show all linked profiles
agentihooks link-profile list

# Remove from chain and clean up symlinks
agentihooks link-profile unlink brain-profile
```

### State

Linked profiles live in `state.json` under a `linked_profiles` array:

```json
"linked_profiles": [
  {"name": "brain", "path": "/abs/path/brain", "linked_at": "<iso>"}
]
```

`_resolve_profile_dir` consults this array as the third lookup tier (after built-in and bundle), so `agentihooks init --profile anton,brain` works as soon as `brain` is registered.

### Stale paths

If a linked profile's directory is later deleted from disk, `agentihooks init` will WARN-skip it and continue with the surviving chain members. The hint message names the exact unlink command:

```
[WARN] Linked profile 'brain' path is missing — run 'agentihooks link-profile unlink brain' to clean up. Skipping.
```

---

## `agentihooks --query`

Print the currently active profile (or chain) and exit.

```bash
agentihooks --query
```

Single profile output:
```
anton
```

Chain output:
```
chain: [coding, anton]
```

---

## `agentihooks status`

Show full system health, MCP fleet inventory with real tool counts, and cost guardrails.

```bash
agentihooks status
```

### What it checks

| Check | What it does |
|-------|-------------|
| **Profile** | Reads `state.json` for active profile and bundle path |
| **Hooks** | Parses `~/.claude/settings.json`, counts hook event entries (expect 10/10) |
| **Python** | Extracts the Python binary from hook commands and verifies it runs |
| **Daemons** | Checks PID file for sync daemon, verifies process is alive |
| **Redis** | Pings Redis, categorizes all `agenticore:*` keys by type |
| **OTEL** | Checks if OpenTelemetry hook telemetry is enabled |
| **Guardrails** | Lists all 8 guardrails with descriptions and enabled/disabled state |
| **MCP** | Reads `~/.claude.json` for all servers, resolves `${ENV_VAR}` auth, queries each HTTP server via MCP protocol for real tool counts, checks per-project blacklists, shows fleet total vs active in current project |

### MCP fleet introspection

The status checker connects to every HTTP MCP server (even disabled ones) to get real tool counts. Auth tokens are resolved from `${ENV_VAR}` references in `~/.claude.json` headers using env vars loaded by `agentienv`. Results are cached at `~/.agentihooks/mcp-tool-cache.json` with a 1-hour TTL.

Per-project blacklists are read from the `projects` block in `~/.claude.json` (the blacklist-all-by-default mechanism). The output shows fleet total (all servers) vs active tools (enabled in current project context).

### In-session skill

The `/agentihooks` skill (delivered via the bundle at `.claude/skills/agentihooks/`) runs the same checker inside a Claude Code session with `--session $CLAUDE_SESSION_ID --json`, adding live session metrics: context fill %, burn rate, per-tool consumption from the context audit, and warning levels.

---

## `agentihooks lint-claude`

Analyze a CLAUDE.md file for token cost and suggest sections to extract into on-demand skills.

```bash
agentihooks lint-claude [path]
```

Defaults to `~/.claude/CLAUDE.md` if no path is given.

### Output

- Total character and token estimate
- Per-section breakdown with classification (always-needed vs workflow-specific)
- Extraction candidates with token savings estimate

---

## `agentihooks extract-skill`

Extract a section from CLAUDE.md into a standalone skill directory.

```bash
agentihooks extract-skill "<Section Heading>" --name <skill-name> [--source <path>] [--output-dir <path>]
```

### Flags

| Flag | Description |
|------|-------------|
| `--name` | Required. Name for the skill directory. |
| `--source` | Path to CLAUDE.md (default: `~/.claude/CLAUDE.md`). |
| `--output-dir` | Output directory (default: source's `.claude/commands/`). |

---

## Standalone Python execution

The hook and MCP server modules can be run directly with Python:

```bash
# Run the MCP tool server
python -m hooks.mcp

# Run with specific categories
MCP_CATEGORIES=aws,utilities python -m hooks.mcp

# Process a hook event manually
echo '{"hook_event_name":"SessionStart","session_id":"test-123"}' | python -m hooks

# Pipe a PreToolUse event
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' | python -m hooks
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Error (installation failed, missing config, etc.) |
| `2` | Block (used by hook handlers to cancel tool execution) |
