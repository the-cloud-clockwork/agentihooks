# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **The recursive-search rewrite no longer reintroduces the prompt it exists
  to remove.** It injected `--exclude=.bashrc` and the other shell-rc
  basenames; Claude Code 2.1.259 parses a Bash option value as a file operand,
  so each one matched a `Read(~/.bashrc)`-style deny rule and asked. The
  injected set is dotenv and credential-file basenames only. Shell rc files
  keep full coverage through `sensitive_kind` (explicit reads, tree-scan
  blocks) and home-wide recursion is blocked rather than rewritten.

### Changed

- **The credential-read guard is operand-aware and fails closed near a
  credential path.** A sensitive path now blocks only when a reader actually
  consumes it — positional operand, `<` redirect, option value, `git` revision
  path (`git show HEAD:.env`, `git diff --no-index .env x`), interpreter
  one-liner, `find … -exec`/`xargs` fed by a matching `-name`. Naming the file
  (`find -name .env`, `grep '\\.env' src.py`, `ls -la .`) no longer blocks; `.`
  and `source` left the reader set (`.` was the search root of every `find .`
  and `grep X .`). A recursive search (`grep -r`, `rg`) over a tree holding a
  credential file is rewritten with `--exclude`/`-g '!…'` via PreToolUse
  `updatedInput` on claude under `bypassPermissions`, and tree-scanned and
  blocked elsewhere. A guard crash while the tool input names a credential
  path raises `BlockAction` instead of logging and allowing; other crashes now
  print the same stderr `WARNING … guard bypassed` as the other guards.
  `hooks/context/credential_guard.py` gained `evaluate()` (`Verdict`) and a
  `main()` so the bundle's `credential-guard.sh` runs the same file as a
  second, deny-only process. Claude Code 2.1.259 widened `Read()` deny
  matching to `grep -r`/`cp -r` operands, which turned the anton profile's
  `Read(//**/.env)` globs into a prompt on every repo search; the deny list is
  now home-anchored only and the guard carries repo-tree coverage.
- **Claude PreToolUse stdout is one JSON envelope.** `emitter.force_buffer()`
  makes `inject_context` buffer on claude for that event, so a secrets NOTE or
  tool-memory block printed ahead of the decision envelope can no longer make
  the host discard it; buffered context is folded into the block message on
  the deny path. `emit_permission_decision` accepts `updated_input` and
  `additional_context`; `hooks.targets.capabilities` lists claude as an
  arg-mutation target (`updatedInput`; copilot keeps `modifiedArgs`).

### Removed

- `ANTON_CREDENTIAL_GUARD`. It was read by the bundle's duplicate copy only;
  `CREDENTIAL_GUARD_ENABLED` is the single switch.

- **Bundles now author settings and MCP in each harness's OWN file format**,
  replacing the Claude-settings translation. codex reads
  `.codex/config.overrides.toml`, copilot reads `.copilot/settings.overrides.json`
  + `.copilot/mcp-config.overrides.json`, claude is unchanged. Native bases live
  at `profiles/_base/{config.base.toml,settings.base.copilot.json}`, and settings
  gain the bundle-global rung MCP already had.

  The translation carried exactly one key (`permissions.defaultMode`) to codex and
  copilot and hardcoded the rest in Python, so every target-native capability was
  unreachable from a bundle. `model_reasoning_effort`, `effortLevel`, per-server
  MCP tool allowlists and OAuth posture are now all profile-settable.

### Fixed

- **Copilot no longer starts an interactive MCP OAuth flow by default.** A 401
  from any http/sse server began a browser authorization that, under WSL, opened
  a Windows browser with no session and hung the turn. `auth`/`oidc` now default
  to false per server; a server that genuinely needs OAuth opts in explicitly.
- **Copilot static-context overflow.** `gateway-tools` alone ships 511 tool
  schemas, putting static context at 121% of the window on copilot's small
  auto-routed models — every simple turn aborted at 0 credits with
  `compaction_static_context_blocked`. The anton profile now ships an exact-name
  `tools` allowlist (99 tools). Note copilot's `tools` does NOT accept wildcards:
  a pattern silently matches nothing and disables the server entirely.

### Added

- **The credential-read guard now runs on every target.** It was wired through
  Claude's settings `hooks` array and paired with Claude-only `permissions.deny`
  rules, so codex and copilot had no credential-read protection whatsoever — and
  copilot cannot supply it through settings either, since its
  `permissions.allow/ask/deny` are enterprise-managed and inert in user settings
  (verified live on v1.0.80: a bare `read` deny did not block a read). Ported to
  `hooks/context/credential_guard.py`, called from `on_pre_tool_use`, gated by
  `CREDENTIAL_GUARD_ENABLED`. The bundle hook's 45-case selftest became a real
  pytest suite. Verified through each target's live entry point.
- Native codex + copilot settings for the `smith` profile.

### Removed

- **`call_agent`, `pool_list`, and `pool_status` are gone from `hooks-utils`.**
  Directed agent-to-agent messaging and the live-agent pool are removed along
  with their backing modules. Coordinate through `channel_publish` /
  `channel_list` / `channel_acknowledge` instead. `channel_acknowledge` is
  unaffected and keeps its explicit `session_id` argument.

## [2.2.0] - 2026-08-20

### Fixed

- Copilot hook dispatch against the REAL v1.0.80 stdin contract, settled from
  live authenticated sessions: no event-name field on stdin (event now passed
  as wrapper argv → `AGENTIHOOKS_COPILOT_EVENT`), preToolUse's batched
  `toolCalls[]` with stringified args and copilot tool names translated onto
  the Claude vocabulary the guardrails read — previously every copilot
  preToolUse guardrail received empty inputs and allowed everything.
- Copilot now translates claude's `permissions.defaultMode: bypassPermissions`
  into `COPILOT_ALLOW_ALL=1`, written to a managed `~/.agentihooks/copilot.env`
  that the installer's `agentienv` shell block already auto-exports. Copilot has
  no settings key for YOLO: `permissions.allow` rules cover tools only (a `write`
  rule still hits path verification) and `trustedFolders` — which the adapter
  previously wrote — is not a recognized user setting at all, so that write was a
  no-op the CLI warns about. Stale `trustedFolders` values are withdrawn on teardown.
- Copilot http MCP headers with a `${VAR}`/`$VAR` reference now resolve
  from the install environment and are written as the literal value copilot
  needs (it has no runtime header expansion) — the same value `~/.claude.json`
  holds. An unset variable keeps the drop-and-warn. Fixes gateway-tools
  failing to connect (401) under copilot because its bearer header was dropped.
- Copilot builtin write tools `apply_patch` and `str_replace_editor` now
  map onto the Write/Edit secrets branch — unmapped they skipped scanning
  entirely (a HARD FLOOR bypass on non-default model backends, caught by an
  adversarial refuter against the shipped binary's write-tool set).
- Copilot block channels per live semantics: exit 2 denies preToolUse only;
  `{"decision":"block","reason"}` on stdout is the sole userPromptSubmitted
  block channel and is now emitted on the BlockAction path.
- Copilot context injection: flush as top-level `additionalContext` — the
  nested claude envelope is silently ignored by copilot's parser.
- Copilot settings managed-key record moved to a `.agentihooks-managed.json`
  sidecar (in-file key triggered a per-launch "unknown top-level key" warning);
  legacy in-file records migrate on init.
- Copilot agent translation: scoped Claude grants (`Bash(git diff*)`) reduce to
  the bare mapped tool; claude model aliases dropped (per-run "model not
  available" warning otherwise).
- Copilot transcript reader: hook-injected context (`user.message` with
  `source:"system"`) no longer counts as a user turn.


### Added

- **Teardown never destroys operator content it cannot attribute.** Findings
  from an adversarial refuter run against the initial teardown, each fixed and
  regression-tested: a persona file with the managed header but no managed-end
  marker is preserved whole as a timestamped backup instead of deleted (the
  operator tail below the missing marker was unrecoverable); an unparseable
  hooks file is backed up, never treated as empty and removed; with no
  install record, `hooks-utils` is removed only when its content proves it is
  ours — a name collision with an operator's own server survives with a
  warning, and codex's `approval_policy`/`sandbox_mode` are left in place with
  a loud review warning rather than a false "[RM] Removed" line; the shared
  `~/.agents/skills` sweep is ledger-only, so an operator's hand-made symlink
  into agentihooks' source tree is never claimed by destination alone.

- **`teardown()` on the target adapters, wired into `agentihooks uninstall`.**
  Uninstall previously removed only `~/.claude` artifacts; codex and copilot
  installs survived a green uninstall in full. Each non-claude adapter now
  removes what it wrote — wrapper script, its hook entries (operator hooks
  kept), the managed persona region (operator tail kept), translated
  prompts/agents/commands via their manifests, managed settings keys (a
  hand-edited value survives), and the MCP servers it recorded installing —
  and the shared `~/.agents/skills` symlinks are swept once by ledger.
  Adapters record their MCP names under `targets.global.<target>.managed_mcp`
  at register time so teardown removes exactly what was installed.

- **Deny-on-doubt for garbled tool-permission payloads.** Hooks stay fail-open
  on unparseable stdin — except when the raw text visibly names a
  tool-permission event (`preToolUse`/`preMcpToolCall`/`permissionRequest`,
  either spelling), which now exits 2 instead of 0. Copilot's tool gate is
  fail-closed on non-zero exit, so exiting 0 there read as "allowed" for a
  tool call no guardrail ever scanned. Non-tool events keep the fail-open rule.

- **MCP `url`, `command` and `args` are now secret-scanned on every target.**
  A credential embedded in a server URL (`https://user:TOKEN@host`,
  `?api_key=TOKEN`), the command, or an argv element previously reached
  `~/.claude.json` / `config.toml` / `mcp-config.json` verbatim. A hit drops
  the whole server with a warning naming the field — a URL credential cannot
  be redacted without breaking the entry.

- **Claude's `settings.json` env block is scanned.** `ClaudeAdapter` copies the
  rendered settings dict verbatim, so a connector-injected literal credential
  in a profile's `env` block landed on disk; values are now scanned and
  credential-shaped literals dropped. `${VAR}`/`$VAR` references pass through.

- **Unbraced `$VAR` references are handled.** Codex maps `Bearer $VAR` to
  `bearer_token_env_var` like the braced form; other unbraced references in
  headers are dropped with a warning on codex and copilot (neither CLI expands
  them, so the literal string was a silently broken config). Scanning
  deliberately strips only the braced form: stripping `$VAR` too ate the tail
  of `pa$sword`-style literal credentials, splitting them below the patterns'
  minimum lengths — a bypass an adversarial refuter reproduced end-to-end.
  `${VAR:-default}` substitutes its fallback text for scanning, so a literal
  token hiding in a default is still caught.

- **Claude's MCP merge sanitizes `env` and `headers` values.** Codex and
  copilot always scanned both; claude wrote them to `~/.claude.json` verbatim,
  so the same profile `.mcp.json` got asymmetric protection per target.
  Field-level drop with a warning; `${VAR}` references pass through (claude
  expands them at connect time).

- **The garbled-payload sniff reads only the payload head.** An unanchored
  search over the full stream let a marker embedded deep inside a garbled
  non-tool event (a raw sample payload in a debug field) deny a SessionStart.
  The event name sits among the first keys of every real payload shape, so a
  512-byte window keeps the truncated-tool-call catch and drops the false
  block.

- **GitHub Copilot CLI is a third install target.** `agentihooks init --target
  copilot` writes `~/.copilot`: `settings.json` managed keys (command-backed
  status line, `disableAllHooks` pinned false, trusted-folder seeding),
  `hooks/agentihooks.json` wiring 12 lifecycle events to a wrapper that sets
  `AGENTIHOOKS_TARGET=copilot`, `copilot-instructions.md` as the compiled
  persona, `mcp-config.json` for MCP, agents translated to Copilot's custom-agent
  registry, and commands translated to skills (Copilot has no prompt-file
  mechanism). `agentihooks doctor --target copilot` reports install health.

  Copilot's hook surface is wider than codex's: `PreToolUse` carries
  allow/deny/ask plus `additionalContext` and argument mutation, native
  `PostToolUseFailure` and `Notification` events remove the need for a
  `notify` shim, and its MCP client speaks SSE. Its transcript
  (`session-state/<id>/events.jsonl`) is a third format the unified reader now
  parses, and unlike codex it states tool success explicitly, so transcript
  error scanning works there.

  The bundle needs no changes — it ships Claude-shaped content once and each
  target's adapter re-projects it. See
  [COPILOT-COMPAT](docs/reference/COPILOT-COMPAT.md).

- **`docs/reference/CODEX-COMPAT.md`** — the codex target's design note,
  reconstructed from the implementation. Six source comments referenced a path
  that did not exist on disk; all now resolve.

### Changed

- **Single-envelope stdout is a target capability, not a codex identity check.**
  The nine `is_codex()` branch sites that meant "this host parses stdout as one
  JSON object" now call `buffers_single_envelope()`, which is true for codex and
  copilot. The two sites that genuinely mean *codex the format* still say so.

- **Persona assembly is shared.** `scripts/targets/_common.py` now holds
  `build_persona()` / `write_persona()` and the identity preamble, used by both
  the codex and copilot adapters, so the doctrine and the operator-tail
  preservation rules cannot drift between targets.

### Fixed

- **Copilot event names spelled PascalCase reached no handler at all.** The
  adapter registers hooks under the PascalCase aliases, but the normalizer's
  map held only camelCase keys — so a payload echoing the registered spelling
  fell through to `EVENT_HANDLERS.get("Unknown")`, logged, and exited 0. That is
  a silent bypass of every guardrail (secrets, branch guard, prod lockdown,
  kubectl guard) for that event. Both spellings now resolve, `PostToolUseFailure`
  included, and a test asserts every registered event maps to a real handler.

- **A present-but-empty payload key shadowed the real value.** The camelCase →
  snake_case fill used `setdefault`, so a copilot payload carrying both
  `toolArgs` and an empty `tool_input` kept the empty one — the tool call
  executed normally while every guardrail read blank arguments.

- **`requires_envelope_block()` was declared and never called.** Copilot's
  runtime calls exit 2 a warning on some events; the mitigation for that was
  dead code. The block path now also emits a
  `permissionDecision: "deny"` envelope for copilot on the events that carry a
  decision field. Codex and claude block paths are unchanged.

- **A translated command could permanently shadow a real skill of the same
  name.** `~/.agents/skills` is shared: copilot writes translated commands there
  as real directories, and the skills symlinker correctly refuses to replace a
  non-symlink — so a name moving from command to skill was stranded, with no
  automatic recovery. Both adapters now clear a translated command whose name a
  real skill claims, before symlinking.

- **The hook timeout field was written as `timeoutSec`**, which appears nowhere
  in the shipped Copilot package; it is now `timeoutSeconds`, the spelling
  present in both `app.js` and the native engine. The loader tolerates
  unrecognized keys silently, so the wrong spelling failed open rather than
  erroring.

- **MCP `tools` entries were written unscanned** on the copilot target — a new
  write surface with no codex equivalent. Each entry is now secret-scanned like
  env and header values.

- **A value containing a `${VAR}` reference skipped secret scanning entirely.**
  Both the codex and copilot MCP writers treated "contains `${`" as "is a bare
  reference, nothing to scan", so `${SAFE_VAR}-and-<literal token>` reached
  `config.toml` / `mcp-config.json` unredacted. References are now stripped and
  the remainder is scanned; a value that is nothing but references still passes
  through untouched. Found by an adversarial refuter, not by the tests.

### Notes

- Codex support was never changelogged when it shipped; the only prior mention
  is a bugfix line under 2.1.1. It arrived as the `TargetAdapter` seam that this
  release's copilot target reuses.

## [2.1.1] - 2026-08-15

### Fixed

- **`agentihooks prune` could never remove a stale MCP server.** The ledger it
  works from (`managed_mcp_servers` in `~/.agentihooks/state.json`) was never
  written — the function that fills it had no callers. An empty ledger made
  every pre-existing server in `~/.claude.json` read as the operator's, latched
  it into `foreign_mcp_servers` permanently, and left the orphan sweep with no
  candidates, so prune always reported "everything is clean". A server dropped
  from a profile `.mcp.json` survived in `~/.claude.json` indefinitely.

  The user-scope merge now claims what it writes and clears the matching foreign
  marks; a pre-existing entry whose config already matches the one being
  installed is treated as agentihooks', not the operator's.

### Added

- **`agentihooks prune --all`** — sweeps user-scope MCP servers that no source
  file defines, ignoring provenance. The escape hatch for entries stranded
  outside the ledger by the bug above.

## [2.1.0] - 2026-08-12

### Removed

- **`agentihooks sessions` (list / reopen / backfill / reconcile).** Claude Code
  now ships its own session picker and resume flow, which supersedes the
  homegrown crash-recovery registry CLI and its Windows Terminal / Terminal.app
  relaunch shim. Reopen a session with Claude Code's native `/resume` or
  `claude --resume`.

  The underlying session registry (`~/.agentihooks/active-sessions.json`) is
  unchanged and still load-bearing — broadcast delivery, the agent pool, and
  `refresh-rules` all read it. Only the operator-facing CLI surface is gone,
  along with `broadcast.derive_session_title` and the heartbeat's
  reconcile-live-sessions bridge, which existed solely to render that listing.

### Fixed

- **The codex adapter hardcoded `http://` in the hooks-utils MCP url.** It
  re-derived the daemon url instead of reusing the builder the claude path
  already had, reintroducing a defect that target had closed (Sonar
  `python:S5332`). It now calls `_build_mcp_config` and takes the url from
  there, so `MCP_SCHEME` / `MCP_PORT` are honoured on both targets — an
  operator fronting the daemon with TLS, or moving it off loopback, gets an
  `https://` client url instead of a hardcoded plaintext one.

## [Unreleased]

> These entries predate the `2.0.0` tag and were never stamped into a released
> section. They ship in `2.0.0` and earlier; they are not part of `2.1.0`.

### Added

- **`agentihooks init` now owns the hooks-utils daemon, and `agentihooks mcp
  start|stop|restart|status` drives it.** In network-transport mode init ends on a
  running daemon serving the config it just wrote — no manual start step, on any
  machine.

  Two supervisors, chosen automatically: **systemd** where a user session exists,
  and a **pidfile backend** everywhere else — WSL2 without `systemd=true`,
  containers, macOS. The fallback runs a detached child recorded in
  `~/.agentihooks/mcp-daemon.pid`, logging to `~/.agentihooks/logs/mcp-daemon.log`.
  It has no supervisor behind it, so it does not survive a reboot; `agentihooks mcp
  start` is then a once-per-boot step. `AGENTIHOOKS_MCP_SUPERVISOR` forces either
  backend.

  Liveness is not a bare `kill -0`: the pid's `/proc` cmdline must still name
  `hooks.mcp`, so a recycled pid cannot read as a running daemon and get signalled.

  **The restart on init is unconditional, and that costs something.** Every
  `agentihooks init` drops each live hooks-utils connection for about a second,
  including a re-run that changed nothing — and because the install is global, that
  is every Claude Code session on the machine, not only the project you ran it from.
  The alternative, comparing every input that could have changed, is more failure
  surface than the restart costs and its failure mode is silent.

  `agentihooks mcp status` is the new diagnostic: configured transport and endpoint,
  what `~/.claude.json` declares, supervisor, process state, port state, and every
  mismatch between them named. Exit codes 0 running and matching, 1 stopped, 2
  diverged.

  *Note on doctrine, stated plainly rather than favourably:* `scripts/sync_daemon.py`
  was deleted in v1.11.3 and `agentihooks init` declared the sole entry point. This
  change does re-adopt two things from it — a background process with pid/lock/log
  state under `~/.agentihooks/`, and "restart the daemon on init", which that
  changelog entry lists by name as a sync_daemon feature. The kinship is real and
  worth saying out loud.

  What is not re-adopted is the part that caused the bug class: sync_daemon **watched
  install-pipeline files and re-ran `_install_global_inner` on its own**, mutating
  settings with no operator action, which is how a chained profile silently demoted
  itself. This daemon runs only `python -m hooks.mcp`, serves MCP tool calls, and
  writes no settings — and it starts only when an operator runs `init` or
  `agentihooks mcp start`. The line that matters is *does it change your config
  behind your back*, not *is it a background process*.

- **`hooks-utils` can run over `sse` / `streamable-http` instead of stdio.**
  Some Claude Code deployments filter every stdio-transport MCP server out at
  load time, taking the whole toolbelt with them. `MCP_TRANSPORT` selects the
  transport (`stdio` default, so nothing changes unless you opt in);
  `agentihooks init` then writes a `url` entry instead of `command`/`args` and
  installs a systemd `--user` unit, which it never starts. Reverting to `stdio`
  restores the stdio entry and removes the unit. See
  [MCP Transport](docs/hooks/mcp-transport.md).
- **The four session-scoped MCP tools take an explicit `session_id`** —
  `call_agent`, `pool_list`, `pool_status`, `channel_acknowledge`. A network
  server is one process serving every session, so no environment lookup can
  identify the caller. SessionStart now names the session's own id
  (`MCP_SESSION_ID_BANNER_ENABLED`, default on). Under stdio the argument stays
  optional; under a network transport the environment fallback is disabled and
  an omitted argument fails rather than acting as whichever session the daemon
  happened to inherit.

### Fixed

- **The network-transport url now names `localhost`, not `127.0.0.1`.** Verified
  on a Claude Code Enterprise machine: the dotted-quad entry was dropped from the
  client's configured-server set outright — absent from `claude mcp list`, and
  `claude mcp get hooks-utils` reported it as not configured — while the
  byte-identical entry spelled `localhost` connected against the same daemon, port
  and transport. Both address the loopback interface, so the access boundary is
  unchanged. `MCP_HOST` defaults to `localhost` for both the bind and the url so
  the two cannot resolve differently.
- **Session identity was resolved from a variable nothing sets.** The code read
  `CLAUDE_SESSION_ID`; Claude Code injects `CLAUDE_CODE_SESSION_ID`. Under stdio
  this silently degraded four tools: `pool_status` failed every call,
  `pool_list` could not exclude self, `call_agent` lost caller attribution, and
  `channel_acknowledge` recorded the ack against the literal string `"unknown"`
  — so a persistent broadcast kept re-injecting for every real session forever.
- **`mcp[cli]` had no upper bound.** `mcp` 2.0.0 removes `mcp.server.fastmcp`
  outright, so a fresh resolve broke `hooks/mcp` at import. Pinned `<2.0.0`.
- **The installer's `.env` scan disagreed with the daemon's parser on the same
  file.** It stripped neither an `export ` prefix nor inline comments, and took
  the last duplicate assignment where `hooks/config.py` takes the first. A
  `.env` reading `export MCP_TRANSPORT=sse` therefore resolved to `stdio` —
  silently, exit 0 — writing a stdio entry on the exact client the network path
  exists for. The scan now mirrors `_parse_env_file`, pinned by a parity test.
- **A bad transport value aborted `agentihooks init` midway.** Validation ran at
  the MCP step, after settings, hooks, skills and CLAUDE.md were already
  written but before the state record — leaving a half-installed tree. It now
  runs before anything is written.
- **The systemd unit could report success while serving nothing.** Its
  `EnvironmentFile=` pointed at `~/.agentihooks/.env`, but systemd's parser is
  not a shell and does not strip `export `, so the daemon fell back to stdio,
  exited 0, and `Restart=on-failure` ignored it. The directive is removed —
  `hooks/config.py` already parses that file correctly at import — and the
  transport is baked into the unit from the value `init` validated, so the
  daemon and `~/.claude.json` cannot disagree. `Restart=always` and `Type=exec`
  replace `on-failure` and `simple`; `StartLimit*` moved to `[Unit]`, where
  systemd actually honours it.
- **Reverting to stdio orphaned a running daemon**, and `agentihooks uninstall`
  could report "nothing to uninstall" while leaving an enabled unit running.

- **Built-in features now ship in the wheel and install from `profiles/package/`.**
  `profiles/package/` is the packaged emulation of a `.claude/` tree and the
  Layer 1 (agentihooks built-in) source for the install symlink merge
  (built-in -> bundle -> profile). `agentihooks init` symlinks its
  `rules/` (and, when present, `skills/agents/commands/`) into
  `~/.claude/<kind>/` exactly like the other feature kinds, and records each link
  in the `managed_links` ledger so uninstall reclaims it. Because the directory
  lives under `profiles/`, it is packaged in the wheel — the previous Layer 1
  source, the repo-root `.claude/`, was never packaged, so PyPI installs received
  no built-in features at all. The repo-root `.claude/` is now operator-local:
  untracked and gitignored (root-anchored `/.claude/`, leaving `profiles/*/.claude/`
  tracked), so nothing under it ships or reaches the remote. The only built-in
  feature at present is `rules/agentihooks-toolbelt.md`.

- **Optional bundle-level `CLAUDE.md` shared by every profile.** A file at
  `<bundle>/.claude/CLAUDE.md` is now prepended to `~/.claude/CLAUDE.md` ahead of
  all profile content, so cross-profile directives are written once instead of
  duplicated into each profile's `CLAUDE.md`. The assembled order is
  `bundle shared -> profile(s) -> CI manifesto`; because profile content comes
  later, a profile can still override shared guidance. The block is delimited by
  `<!-- BEGIN BUNDLE CLAUDE.md ... -->` / `<!-- END BUNDLE CLAUDE.md -->` and is
  replaced in place on re-install rather than stacked. It is prepended exactly
  once per install, including for chained `--profile a,b` installs. No-op when
  the bundle has no such file, the file is empty, no bundle is linked, or no
  profile in the chain supplied a `CLAUDE.md` to prepend onto. Markers
  deliberately avoid the `profile:` keyword so the init lost-state guard does not
  read them as a phantom chain member, and avoid the managed-file ownership
  phrase so a bundle block alone never makes a hand-authored `CLAUDE.md` look
  agentihooks-owned. Before mutating a `CLAUDE.md` it does not already own, the
  step backs it up and records the original, so uninstall restores rather than
  deletes. Bundle content carrying any managed marker is refused with a warning
  instead of corrupting the first-occurrence splices. Unlinking the bundle (or
  emptying the file) retracts a previously-prepended block rather than stranding
  it. See `docs/bundles.md`.

### Fixed

- **Wheel packaging dropped every markdown and most profile data.** The
  `package-data` globs shipped only a couple of JSON/YAML files and no `.md` at
  all, so profile personas (`CLAUDE.md`) and rules (`rules/*.md`) never made it
  into the wheel; the `profiles.default` `.claude/CLAUDE.md` glob pointed at a
  path that did not exist, and `profiles.admin` / `profiles.coding` entries named
  directories that were already gone. Packaging now ships every `.json`, `.yml`,
  `.yaml`, and `.md` anywhere under `profiles/` (personas, rules, settings,
  manifests) via recursive globs, and nothing else; the stale and broken entries
  are removed.
- **`prune`, `uninstall`, and `init --force` removed artifacts agentihooks never
  installed.** All three inferred ownership at deletion time — by path prefix, or
  by "not currently defined by a profile" — instead of reading what was recorded
  at install time. `prune` deleted every MCP server in `~/.claude.json` that no
  agentihooks source defined, including every server added with `claude mcp add`.
  `init --force` removed `~/.claude/{rules,skills,agents,commands}` wholesale,
  taking third-party symlinks with them, and deleted `settings.local.json`, which
  agentihooks never writes. Every `init` also reaped any dangling symlink in those
  directories regardless of owner, so an unmounted share was indistinguishable
  from a dead link.

  Ownership is now **recorded at creation**: every symlink install writes is
  tracked in `state.json` under `managed_links` with its target, and removal
  touches only what is recorded — or what points into a directory agentihooks
  demonstrably links out of (`<root>/.claude/<kind>/` and
  `<root>/profiles/<name>/.claude/<kind>/`, matched lexically so a moved or
  deleted source is still recognised). Registered roots are no longer trusted
  wholesale: a bundle or linked profile registered one level too shallow used to
  claim every symlink beneath it. MCP removal is scoped to
  `state['managed_mcp_servers']`, and a profile defining a name the operator had
  already configured no longer overwrites it, is recorded in
  `foreign_mcp_servers`, and is never claimed by the ledger — previously that
  collision escalated from a silent overwrite to a silent deletion on the next
  profile update. `uninstall` now restores the pre-agentihooks `settings.json`
  from the backup taken at first install, matching what `CLAUDE.md` already did,
  and no longer reports "nothing to uninstall" while the `~/.bashrc` block and the
  CLI are still present.

  Also fixes `agentihooks prune` aborting mid-run: its "what do we manage" helper
  guarded with `except Exception`, which does not catch the `SystemExit` raised
  when the MCP config builder cannot resolve a Python — a moved venv sufficed. It
  now reports unknowable distinctly from empty, and the orphan sweep declines to
  run rather than treating "we define nothing" as "delete everything we
  installed".

- **`~/.claude/CLAUDE.md` backup churn on every `agentihooks init`.** The profile
  writer's "already up to date" check compared profile-only content against a
  file that also carries the appended CI-manifesto block, so it could never match
  once the manifesto existed — every re-run took the backup+overwrite path and
  dropped another `CLAUDE.md.bak.<timestamp>` into `~/.claude`. The check now
  strips managed blocks before comparing. Pre-existing since the manifesto append
  landed; surfaced while adding the bundle-level block.

### Changed

- **CI now gates `dev`.** Every workflow triggered only on `pull_request` to
  `main` or `workflow_dispatch`, so the branch agents commit to and deploy from
  had no gate — the suite ran only once a snapshot PR reached `main`, long after
  the code shipped. `test.yml` and `ci.yml` now also run on push to `dev`. The
  Tests job runs the full suite rather than `pytest -m unit`, which collected 439
  of 999 tests and excluded every ownership-scope test. Tools are invoked as
  `python -m <tool>`: on the self-hosted runner `pip` installs into the user site,
  whose `bin` is not on `PATH`, so bare `ruff`/`pytest` failed with exit 127.

- **`hooks-utils` MCP server slimmed to two agentihooks-native categories.**
  Removed the generic cloud-utility categories (`aws`, `compute`, `database`,
  `email`, `observability`, `storage`, `utilities`) and their modules; the
  server now ships only `channels` (fleet broadcast + brain) and
  `enforcement` (doctrine banners) — 9 tools, down from ~22. `MCP_CATEGORIES=all`
  expands to the trimmed registry, so existing installs pick up the smaller
  surface on next MCP restart. Docs, README, SECURITY, and CONTRIBUTING updated
  to match. `build_server()` now warns on stderr for unknown categories and for
  a zero-tool server instead of failing silently. The removed categories'
  underlying integrations (`hooks/integrations/`) are unchanged.

### Fixed

- **`agentihooks init` again cleans up MCP servers dropped from a profile or
  bundle.** Two regressions introduced when the sync daemon was deleted
  (`8ea78ba`) are fixed:
  - `_collect_all_managed_mcp_servers()` / `_reseed_managed_mcp_sources()`
    resolved the active profile by passing the whole comma chain
    (`"anton,brain"`) to `_resolve_profile_dir`, which returns `None` — so the
    "managed" set silently collapsed to just `hooks-utils`. They now walk the
    full chain via `_resolve_profile_chain()` (plus the settings-profile
    overlay).
  - The orphan-prune that used to run in the daemon reconcile loop was never
    re-wired into `init`, so `init` had become additive-only. It now reconciles
    a persisted ledger (`state.json['managed_mcp_servers']`): servers agentihooks
    installed on a prior run but no longer present in any source are removed from
    `~/.claude.json`. **Hand-added servers are never touched** (they are not in
    the ledger) — `agentihooks prune` remains the explicit sweep for genuine
    cruft. `uninstall` removes the full managed set ∪ ledger, and the `prune`
    CLI summary now counts orphaned/enabled removals (previously it printed
    "everything is clean" after removing orphaned `mcpServers`).

### Removed

- **Per-repo `.agentihooks.json`, `profile.yml`, and the runtime overlay
  system — all gone (2026-05-07).** `agentihooks init` is now global-only:
  no `--repo`, no `--local`, no per-project `settings.local.json` writer,
  no `.agentihooks.json` reader anywhere in the codebase. `profile.yml` is
  no longer read — `description`, `mcp_categories`, `enabledMcpServers`,
  `allowedOverlays`, and the `claude:` block are gone (mcp_categories
  hardcoded to `"all"`; `cmd_claude` now passes only
  `--dangerously-skip-permissions`; the blacklist-by-default sweep is
  removed and confirmed obsolete — Anthropic lazy-loads tool schemas, overhead
  is ~20k with all MCPs active vs ~200k before the fix). The runtime overlay system (`scripts/overlay.py`,
  `hooks/context/overlay_injector.py`, `hooks/mcp/profiles.py`,
  `agentihooks overlay` CLI, `OVERLAY_INJECTION_ENABLED`,
  `AGENTIHOOKS_AUTO_OVERLAY`, `~/.agentihooks/active_overlays.json`,
  statusline `overlay:` column) is removed. Channel `subscribe` /
  `unsubscribe` (CLI + MCP tools) removed; every session is hard-coded
  to `BASE_CHANNELS = ("brain", "amygdala")`. OTEL helper
  (`_build_otel_env`) retained for future re-wiring; OTEL env injection
  no longer reads from `profile.yml`.
- **Sync daemon (`scripts/sync_daemon.py`) — deleted entirely.** The auto-init loop (file-hash watcher → `_install_global_inner`) was the root cause of the chain-demotion bug class fixed across v1.11.2 → v1.11.3. `agentihooks init` is now the sole entry point that re-applies profile/bundle/MCP changes; it's idempotent and reads `state.json`. Also removed: `cmd_daemon` and the `agentihooks daemon` subcommand, daemon restart in `cmd_init`, daemon stop in `cmd_uninstall`, daemon liveness checks in `status_checker`, `tests/test_sync_daemon.py`, the `AGENTIHOOKS_SYNC_POLL_SEC` env var, and all heartbeat / hash-manifest / crash-sentinel state files. Old artifacts (`sync-daemon.pid`, `sync-daemon.heartbeat`, `.sync-daemon.singleton.lock`) are now scrubbed by `agentihooks uninstall`.

### Changed

- **Channel subscriptions are env-driven** — the `BASE_CHANNELS` constant in `hooks/context/broadcast.py` and its duplicate in `hooks/statusline.py` are gone. Subscriptions now come from the `AGENTIHOOKS_BASE_CHANNELS` env var (comma-separated), parsed once at `hooks.config` import time. Layered via Claude Code's native settings.json `env` block: profile default (ships `"brain,amygdala"` in `profiles/default/.claude/settings.overrides.json`) → repo `.claude/settings.json` → repo `.claude/settings.local.json` → container launch ENV. Empty / unset → session only receives global broadcasts. No code-level channel name fallback — channels are policy, not implementation.
- **MCP prune helpers** — `_get_valid_mcp_names` / `_prune_stale_mcp_servers` moved from `sync_daemon.py` into `scripts/install.py` near `cmd_mcp` (used by `agentihooks mcp prune`). No behavior change.
- **`broadcast.heartbeat_sessions()` now runs on SessionEnd** — `hook_manager.on_session_end` calls it after deregister so dead session entries are pruned on every clean shutdown. Previously only the daemon called it.
- **Memory-mirror `tick()` is now manual.** New CLI: `agentihooks memory tick` runs one consume + (if authority) push to `origin/main`. Hooks continue to call `pull_only()` automatically on session events via `hooks/context/memory_sync_events.py`.

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
