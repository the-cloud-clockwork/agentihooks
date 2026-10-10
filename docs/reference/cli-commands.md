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

The single entry point for installing agentihooks. Handles global setup and bundle linking.

```bash
agentihooks init [--bundle <path>] [--profile <name>]
```

### What it does

1. Links bundle directory (if `--bundle` is provided)
2. Merges settings in the target's own format: `_base/<target base>` -> bundle `.<target>/` -> profile `.<target>/` overrides
3. Substitutes `/app` -> real repo path and `__PYTHON__` -> venv Python in all commands
4. Preserves personal keys (`model`, `autoUpdatesChannel`, `skipDangerousModePermissionPrompt`) from any pre-existing unmanaged settings
5. Writes `~/.claude/settings.json` with hook wiring and tool permissions
6. Symlinks skills, agents, commands, and rules via 3-layer merge (agentihooks built-in -> bundle global -> each profile in chain)
7. Writes `~/.claude/CLAUDE.md` -- single profile: file copy; chained profiles: concatenated with `---` separators and `<!-- profile: name -->` markers
8. Installs MCPs (agentihooks + bundle `.claude/.mcp.json` + profile `.claude/.mcp.json`)
9. Installs the `agentihooks` CLI globally via `uv tool install --editable .`
10. Writes managed bashrc block (`agentienv` function + `agenti` alias)

Step 8 has one extra behaviour when `MCP_TRANSPORT` names a network transport:
it renders the systemd unit and **starts the agentihooks daemon**, restarting it
unconditionally. Reverting to `stdio` stops the daemon and removes the unit, so a
downgrade cannot leave a process serving a port nothing points at. See
[`agentihooks mcp`](#agentihooks-mcp) and
[MCP Transport]({{ site.baseurl }}/hooks/mcp-transport/).

> Per-repo init (`--repo` / `--local` / `.agentihooks.json`) was removed
> 2026-05-07. `agentihooks init` is global-only.

### Flags

| Flag | Description |
|------|-------------|
| `--bundle <path>` | Path to bundle directory. First-time: links the bundle and runs global install. |
| `--profile <name>` | Profile to install. Comma-separated for chaining: `--profile coding,anton` (default: `default`, env: `AGENTIHOOKS_PROFILE`) |
| `--force` | Clean install — resets install state (`state.json`, sync hashes, session caches, PID files, `prod_bypass/`, `controls_flags/`, `voice_flags/`, `force_refresh/`) and re-symlinks `~/.claude/` assets. **Preserves** broadcasts, enforcements, brain data, logs, quota accounts, `.env`, `.venv`. |

### Environment variables

| Variable | Description |
|----------|-------------|
| `AGENTIHOOKS_PROFILE` | Default profile when `--profile` is not passed (default: `default`) |
| `AGENTIHOOKS_SETTINGS_PROFILE` | Default settings-only overlay profile (default: none) |
| `AGENTIHOOKS_MCP_FILE` | Path to an MCP JSON file to auto-merge into `~/.claude.json` during install |
| `CLAUDE_CODE_HOME_DIR` | Home-directory root override -- `.claude` is appended automatically (default: `$HOME`) |
| `AGENTIHOOKS_CLAUDE_HOME` | Legacy: direct path to the `.claude` directory (default: `~/.claude`) |
| `AGENTIHOOKS_HOME` | Override the agentihooks state directory (default: `~/.agentihooks`). Used for per-pod isolation on shared filesystems — set to `/shared/.agentihooks-<pod-name>` so each pod gets its own state without racing on `state.json`. |
| `AGENTIHOOKS_MCP_TRANSPORT` | One-off override of `MCP_TRANSPORT` for a single `init`, without editing `.env`. |
| `AGENTIHOOKS_MCP_SUPERVISOR` | `auto` (default), `systemd`, or `pidfile`. Forces the daemon backend instead of probing for a systemd user bus. |

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
| `NAME` | Settings profile to apply. Only its settings/MCP files for the active target are used, not its persona or content. |
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

## `agentihooks channel`

Channel-scoped broadcast publishing and inspection. A session only receives a channel-tagged message if its subscription list (from `AGENTIHOOKS_BASE_CHANNELS`) includes that channel. Global broadcasts (no channel) reach everyone.

### Subcommands

```bash
agentihooks channel publish CHANNEL MESSAGE [-s SEVERITY] [-t TTL]
agentihooks channel list
```

| Subcommand | Purpose |
|---|---|
| `publish` | Publish a message to a named channel. Same severity tiers as `broadcast` (`info` / `alert` / `critical`). |
| `list` | Show active channels in `~/.agentihooks/broadcast.json` with message counts per channel. |

### Subscriptions live in `settings.json`, not in CLI

There is intentionally no `channel subscribe` or `channel unsubscribe` subcommand. Subscriptions are operator-configured via the `AGENTIHOOKS_BASE_CHANNELS` env var, set in the profile's `settings.overrides.json` `env` block (or per-repo via `.claude/settings.local.json`, or per-container via launch ENV). See [Broadcast System → Channel Subscriptions](../hooks/broadcast.md#channel-subscriptions).

### Examples

```bash
# Tagged broadcast — only sessions subscribed to "deploy" see it
agentihooks channel publish deploy "Image roll-out paused on cluster-west" -s alert -t 1h

# Knowledge / brain content (the brain adapter publishes here automatically)
agentihooks channel publish brain "Hot arcs updated: 3 active" -s info -t 4h

# Inspect what's flowing
agentihooks channel list
```

---

## `agentihooks enforcement`

Manage recurring reminders injected every N tool calls. Global runtime entries live in `~/.agentihooks/enforcements.json`; `--local` scopes the operation to `<git-root>/.agentihooks/enforcements.json`.

```bash
agentihooks enforcement set "run tests before committing" 5
agentihooks enforcement set --local "read the operator directory" 10
agentihooks enforcement set "cluster writes go through GitOps" 1 --matcher bash.kubectl
agentihooks enforcement set --type rule --path rules/deploy.md
agentihooks enforcement list [--local]
agentihooks enforcement clear [--local] [--id <id> | --tag <tag>]
```

`--local` requires a Git project. Local files use the same JSON schema and cadence behavior as the global store. AgentiHooks creates the resource directory on the first local `set`, never edits Git ignore configuration, and leaves the directory in place after `clear`.

During injection, project-local entries are added to bundle, profile, and runtime entries. A matching local ID has highest precedence. The MCP tools take the same scope as `local=true`, resolved from the session's project directory (or an explicit `cwd`).

`--matcher` limits an entry to matching tool calls (`bash`, `bash.git`, `edit+write`, `mcp`, `mcp__<server>`); its cadence then counts matching calls only. Grammar: [Conditions](../hooks/conditions.md#matcher-grammar).

`--type rule --path <file>` stores the canonical absolute path and reads the
complete current UTF-8 file on every injection. Relative CLI paths resolve from
the current directory. The MCP `enforcement_set` surface accepts the same `type`
and `path` fields but requires an absolute path.

---

## `agentihooks conditions`

Inspect the [conditions](../hooks/conditions.md) the running harness would execute.

```bash
agentihooks conditions list                                   # layers, conditions in order, invalid files
agentihooks conditions list --step post
agentihooks conditions list --tool Bash --command "cd x && git push"
```

The profile chain is read for `AGENTIHOOKS_TARGET` (default `claude`).

---

## `agentihooks claude` / `agenti`

Launch Claude Code on one of the `AH_CC_TOKEN_<slug>` accounts, with
`--dangerously-skip-permissions` and every other flag passed through. See
[Claude Account Load Balancing](../pillars/load-balancing.md).

```bash
agenti                          # fewest live sessions among accounts below their band cap
agenti --route work             # force AH_CC_TOKEN_work, ignore the cap
agenti --model fable            # Fable models also weigh the separate Fable quota
agenti --route api              # force the api side; fails when no api endpoint is configured
```

The launch line reports `account`, `routing_left` and `sessions=n/cap`, or
`account=api kind=api` for the api side. The api weight decides which side a
launch takes; with every account at its cap the launch falls back to the api.
When the api is full or absent too, the launch fails with `no Claude account has
a free session under its quota band`.

## `agentihooks balance`

Probe and rank every Claude account, plus the api row when an endpoint is
configured. Columns: `#`, `ACCOUNT`, `KIND` (`subscription`, `api`), `STATE`,
`SESSIONS` (live/cap), `WEIGHT` (api rows), `CAP`, `ROUTING LEFT` and the five
hour and weekly windows (`n/a` on an api row).

```bash
agentihooks balance                                  # probe (60 s cache) and rank
agentihooks balance --current                        # mark the account this session runs on
agentihooks balance --refresh                        # ignore the cache
agentihooks balance settings                         # every routing setting and the store in use
agentihooks balance set claude-api-weight=25         # live share of the api side, 0 to 100
agentihooks balance set codex-api-max-sessions=4     # api session cap; none clears it
```

`balance set KEY=VALUE ...` takes the keys `claude-api-weight`,
`codex-api-weight`, `claude-api-max-sessions`, `codex-api-max-sessions`,
`master-account-claude`, `master-account-codex`, `master-tier-claude` and
`master-tier-codex`. It validates every pair before writing, prints
`store=redis` or `store=file <path>` and one `key: before -> after` line per key,
and exits 2 on an unknown key or an invalid value. See
[Claude Account Load Balancing](../pillars/load-balancing.md#routing-settings).

---

## `agentihooks init-agent`

Open a routed Claude session in a new terminal (WSL, macOS, native Linux).

```bash
agentihooks init-agent --dir ~/dev/repo --name fix-x --prompt-file task.md -- --model opus
agentihooks init-agent --handoff --dir "$PWD" --name repo-handoff --prompt-file handoff.md
```

| Flag | Meaning |
|---|---|
| `--dir`, `--name`, `--prompt` / `--prompt-file` | Directory, session/tab name, opening prompt |
| `--dry-run` | Print the launch without opening a terminal |
| `--start-timeout` | Seconds to wait for the terminal to start (default 30) |
| `--route-timeout` | Seconds to wait for the new session's route report (default 150) |
| `--handoff` | Quota handoff: any account but this one, no bare-Claude fallback, marks this session handed off once the new one is routed; exit 3 on failure |

Output is `key=value` lines: `status=started`, `route_status` (`routed`, `bare`,
`failed`, `pending`), `account`, `placement`, and `handoff=done|failed` with
`--handoff`.

Folder trust: before a Claude launch, a folder Claude has not trusted (neither it
nor a parent has `hasTrustDialogAccepted` in `$CLAUDE_CONFIG_DIR/.claude.json`,
else `~/.claude.json`) is recorded as trusted, so the session never waits at the
trust question. The launcher pins the session to that same config home: it
exports the caller's `CLAUDE_CONFIG_DIR`, or unsets it when the caller has none,
so a herdr pane's own environment cannot point Claude at another file.
`trust=trusted|marked|untrusted` names the outcome;
`AGENTIHOOKS_TRUST_LAUNCH_DIR=0` turns marking off. On `untrusted` the launch
still goes ahead and stderr says the session waits for someone to answer.

Hosts: herdr when installed and not disabled (`state.json` `herdr.enabled`), else
the native terminal. `--host herdr|native` or `AGENTIHOOKS_TERMINAL_HOST` overrides.
In herdr the session opens as a tab in the caller's workspace, in `--workspace
<label>` (crew), or in the repository's workspace; `--placement split|workspace`
changes that. The output adds `workspace_id`, `tab_id`, `pane_id` and `agent_name`.

Agents: `--agent claude|codex`; without it, the harness of the account the session
rotation picks: the eligible Claude or Codex account with the fewest live sessions
under its band cap (see Load balancing). Codex runs directly (`route_status=direct`).

---

## `agentihooks herdr`

Install and configure herdr, the terminal host `init-agent` opens agents in.
The first `agentihooks init` on a terminal asks once whether to use herdr and
records the answer in `state.json` (`herdr.enabled`); agents pass
`agentihooks init --herdr yes|no`. Later inits with herdr enabled install the
binary when missing and (re)install the herdr integrations for Claude and Codex.

```bash
agentihooks herdr              # status; offers to install when herdr is missing
agentihooks herdr install      # official installer, then configure
agentihooks herdr configure    # herdr integration install claude|codex
agentihooks herdr disable      # init-agent stops choosing herdr
```

## `agentihooks quota`

Quota left for every agent harness: one row per Claude account (router cache, or a
live probe with `--refresh`) and one for Codex, read from the newest rate-limit
event in Codex's session logs.

```bash
agentihooks quota            # AGENT ACCOUNT KIND STATE SESSIONS WEIGHT CAP 5H LEFT 5H RESET 7D LEFT 7D RESET SOURCE
agentihooks quota --json
```

## `agentihooks classify`, `classifier stats`, `classifier eval`

Ask the LiteLLM decision models typed questions, and read the decision log. Details in
[Decision classifier](classifier.md).

```bash
agentihooks classify --state state.json --questions questions.json [--purpose P]
agentihooks classifier stats [--purpose P]
agentihooks classifier eval NAME [--live N]
```

## `agentihooks balance`

```bash
agentihooks balance              # every account: state, SESSIONS n/cap, routing left, resets
agentihooks balance --current    # which account this session runs on
agentihooks balance --refresh    # ignore the 60-second quota cache
agentihooks balance --fable      # include the Fable weekly quota
```

`SESSIONS` counts live interactive Claude processes per account from `/proc`;
sessions on no account are listed under the table as `unrouted`.

---

## `agentihooks gc`, `lease`, `scratch`

Workspace lifecycle for agent-created directories: git worktrees, scratch dirs and tool files.

```bash
agentihooks gc                     # report what is safe to remove
agentihooks gc --enforce           # act on findings that are due: remove, snapshot to wip/, archive
agentihooks gc --json              # full report; also written to ~/.agentihooks/gc-last.json
agentihooks gc --path ~/scratchpad/myrepo
agentihooks gc --install-timer     # hourly systemd user timer (agentihooks install does this when LIFECYCLE_GC_ENABLED)
agentihooks gc --remove-timer
agentihooks lease <worktree>       # record the calling agent session as owner (wt.sh new calls this)
agentihooks lease <dir> --kind scratch|ephemeral
agentihooks scratch new                 # mkdir ~/scratchpad/<repo>/<task> + lease, prints the path; the name is built
                                        # from the session (<swarm>-<task> or its session base), another name is refused
agentihooks name worktree|tmp [--check NAME]  # the built worktree name wt.sh uses: the session base, then -2, -3
agentihooks scratch rm <task-dir>       # safe replacement for rm -rf "$dir": refuses when another
                                        # process works inside, another live session holds the lease,
                                        # or a nested worktree has uncommitted or unpushed work
```

A path is actionable only when it is **unused** and **removing it loses nothing**:

- Unused: no live process has its cwd inside it, no lease holder is alive (pid + `/proc` start time + `boot_id`, or a live session file with the same `sessionId` and matching `procStart`), nothing in it changed recently, and the machine has been up at least 2 h.
- Worktrees: clean and reachable from a remote after 2 h idle (`remove`); dirty or unpushed after 24 h idle (`snapshot` — pushed to `wip/` first). Primary checkouts, `dev`/`main`/`master`, git-locked worktrees and any merge/rebase in progress are never touched.
- Scratch task dirs (`~/scratchpad/<repo>/<task>`): idle past the root's `idle_days`, then the oldest dead dirs while the root is over `budget_gb`. A `.keep` file pins a dir; files at depth ≤ 2 are never touched.
- Injection traces (`~/.agentihooks/injections/<session>.jsonl`): removed once the session is no longer live and the trace was last written more than `AGENTIHOOKS_TRACE_KEEP_DAYS` ago (default: the root's `idle_days`, 14). Liveness is checked again right before removal.
- An action is `due` only after two sweeps at least 1 h apart in the same boot. `--enforce` re-checks each due path against a fresh process snapshot right before acting.
- `snapshot` builds a commit from the worktree through a temporary index (ignored files and files over 50 MB are left out and listed), with `[skip ci]` in the message, and pushes it to `wip/<repo>/<worktree>-<timestamp>`. A failed push keeps the worktree.
- Removals are journaled in `~/.agentihooks/gc-journal.json`; the next sweep finishes any removal a crash interrupted.
- Herdr panes: `init-agent` (every swarm, resume and proof launch) and `run-in-terminal` with a command record each pane they open in `~/.agentihooks/herdr/panes/` (pane id, terminal id, tab, workspace, owner swarm and session, launch time, route status). `gc` lists, and `gc --enforce` and every `swarm tick` close, a recorded pane whose agent is gone: it sits at a bare shell (the agent or command exited, or the launch failed), its swarm agent was retired, or its swarm stopped or was removed. Never inside `AGENTIHOOKS_HERDR_GC_GRACE_MINUTES` (default 5) of launch, while its input line holds text, or while its screen changed or its agent was prompted inside `AGENTIHOOKS_INBOX_QUIET_S`; a pane whose terminal id changed, or a swarm store that cannot be read, keeps the pane. A pane agentihooks never recorded, such as a plain `run-in-terminal` shell, is never touched. A tab or workspace the sweep empties is closed. Each close prints with its reason, `herdr: closed pane …` in the tick journal (`journalctl --user -u agentihooks-swarm.service`).
- Launch run folder (`$XDG_RUNTIME_DIR/agentihooks-claude-terminal`): launch scripts, route reports, prompts, start markers and profile reports older than a day are removed, and closing markers whose launcher has exited.

Triggers: `agentihooks-gc.timer` runs `gc --enforce` hourly, first 2 h after boot. Every PreToolUse call that names a path inside a managed worktree or scratch task dir records the session as a lease holder; while gc is removing that path the call is blocked with a retry message. When free space (the smaller of `$HOME` and, on WSL, `/mnt/c`) drops below `AGENTIHOOKS_DISK_WARN_GB`, tool calls get a warning (at most every 10 min) and a sweep starts at once. SessionStart and SessionEnd start a sweep at most every `AGENTIHOOKS_GC_INTERVAL_MIN`; without systemd, the sweep runs as a detached hook task.

Roots come from `profiles/_base/lifecycle.json`, overlaid by `lifecycle.json` in the bundle, each active profile and `~/.agentihooks/lifecycle.json`, merged by `id` (`"enabled": false` drops a root). Kinds: `worktrees`, `scratch`, `ttl` (delete idle entries), `archive` (gzip idle files matching `include`), `traces` (delete injection traces of ended sessions).

---

## `agentihooks refresh-rules`

Push profile rule updates into every running Claude Code session without a restart. Each target session consumes the refresh once on its next `UserPromptSubmit`.

```bash
agentihooks refresh-rules [--profile <name>] [--dry-run] [--clear]
```

### How it works

1. Reads the installed rules: `~/.claude/CLAUDE.md` and every `~/.claude/rules/*.md`. In a rendered profile home (`CLAUDE_CONFIG_DIR`) it re-renders the profile first; that home has no `rules/` folder, its `CLAUDE.md` carries persona and rules together.
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

## `agentihooks mcp`

Two unrelated jobs behind one word: a surface-area report, and the agentihooks
daemon's lifecycle.

```bash
agentihooks mcp report [--project PATH]   # tool/token surface across every configured server
agentihooks mcp status                    # daemon: configured vs. actually running
agentihooks mcp start
agentihooks mcp restart
agentihooks mcp stop
```

### The daemon subcommands

These do nothing under stdio, which is the default — Claude Code spawns
agentihooks per session and there is no daemon. They exist for the network
transports; see [MCP Transport]({{ site.baseurl }}/hooks/mcp-transport/).

`agentihooks init` starts the daemon itself, so `start` is only needed after a
reboot on a machine with no systemd user session, where the fallback backend has
no supervisor behind it.

### `status` and its exit codes

`status` is the one worth knowing. It prints the configured transport and
endpoint, what `~/.claude.json` declares, which supervisor is in play, whether
the process is up, whether the port answers, and names every mismatch between
them.

| Exit | Meaning |
|------|---------|
| `0` | running, and matching config |
| `1` | stopped |
| `2` | running, but diverged from config |

The divergence case is why this exists. A daemon started before a config change
keeps serving the old transport or port while `~/.claude.json` names the new one,
and nothing else reports it — the only symptom is tools that quietly fail to
appear.

### Supervisors

| Backend | Chosen when | Survives reboot |
|---------|-------------|-----------------|
| `systemd` | `systemctl --user` reaches a user bus | yes |
| `pidfile` | everything else — WSL2 without `systemd=true`, containers, macOS | no |

`AGENTIHOOKS_MCP_SUPERVISOR=systemd\|pidfile` forces one. An unrecognised value
warns on stderr and falls back to detection.

The pidfile backend records `~/.agentihooks/mcp-daemon.pid` and logs to
`~/.agentihooks/logs/mcp-daemon.log`, rolled to `.log.1` past 5 MB.

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
- The agentihooks daemon -- stopped under both backends, and its systemd unit removed. Uninstall verifies afterwards and warns if a process survived, since an orphaned daemon with the CLI gone has nothing left to manage it
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

Launch Claude Code with `--dangerously-skip-permissions` and pass through any extra args.

```bash
agentihooks claude [extra-args...]
```

**Alias:** `agenti` (installed by `agentihooks init` in the bashrc block)

### How it works

The launcher injects exactly one flag: `--dangerously-skip-permissions`. Any extra arguments are appended verbatim. Model, effort, and other Claude Code defaults come from `~/.claude/settings.json` (rendered from each profile's `settings.overrides.json`).

> The `claude:` block in `profile.yml` was removed 2026-05-07; profile-level
> CLI flag mapping no longer exists.

### Examples

```bash
# Launch
agentihooks claude

# Use the alias
agenti

# Pass extra args to claude
agenti --model haiku --verbose
```

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
| **Redis** | Pings Redis, categorizes all `agenticore:*` keys by type |
| **OTEL** | Checks if OpenTelemetry hook telemetry is enabled |
| **Guardrails** | Lists all 8 guardrails with descriptions and enabled/disabled state |
| **Broadcast** | If `BROADCAST_ENABLED=true`, reports active session count and pending message count from `~/.agentihooks/broadcast.json`. Channel subscriptions aren't summarised here — they're visible per-session on statusline Line 3 (read from `AGENTIHOOKS_BASE_CHANNELS`). |
| **MCP** | Reads `~/.claude.json` for all servers, resolves `${ENV_VAR}` auth, queries each HTTP server via MCP protocol for real tool counts, shows fleet total vs active in current project |

### MCP fleet introspection

The status checker connects to every HTTP MCP server (even disabled ones) to get real tool counts. Auth tokens are resolved from `${ENV_VAR}` references in `~/.claude.json` headers using env vars loaded by `agentienv`. Results are cached at `~/.agentihooks/mcp-tool-cache.json` with a 1-hour TTL.

The output shows fleet total (all servers) vs active tools (enabled in current project context). Individual servers can be toggled via the `/mcp` UI — that state is stored in `~/.claude.json`'s projects block.

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
MCP_CATEGORIES=channels,enforcement python -m hooks.mcp

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
