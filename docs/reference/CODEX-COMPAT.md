# CODEX-COMPAT — OpenAI Codex CLI as an AgentiHooks target

Reference for the `codex` install target, and the companion to
[COPILOT-COMPAT.md](COPILOT-COMPAT.md).

**Provenance.** This page was reconstructed from the adapter implementation
(`scripts/targets/codex_target.py`, `hooks/targets/`) and its test suite after
the original design note was lost — six source comments referenced a path that
no longer existed. It records what the shipped code encodes. The facts it
encodes were verified against **codex-cli 0.147.0 (2026-08-10)** and re-verified
in part against **0.154.0 (2026-09-11)**; where a claim is code-derived rather
than re-verified against the binary, §10 says so.

## §1 What a target is

See `scripts/targets/__init__.py`. A target is the agent CLI whose config
surface `agentihooks init` writes. Profile resolution, bundle linking, settings
merging and MCP server-dict assembly are target-agnostic; everything touching a
target-specific path or schema goes through the adapter from `get_adapter()`.

## §2 Hook contract

### §2.1 Events

Wired in `CODEX_HOOK_EVENTS` — the ten events handled by both `codex` `hooks.json`
and `hook_manager.EVENT_HANDLERS`:

`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`Stop`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PermissionRequest`.

Codex 0.160.0 fires `PreCompact`, then `PostCompact`, then `SessionStart`
with `source: "compact"` during automatic compaction. AgentiHooks uses the
registered `PreCompact` to mark refocus pending and compact `SessionStart` to
restore current swarm intent immediately. A master receives the ledger overview,
active phases, priorities and its coordination obligations without a task claim;
a worker retains its phase and task intent. The session refocus state consumes
that pending refresh once. Ordinary startup keeps refocus on the first prompt.

`PostCompact` is deliberately unregistered: compact `SessionStart` covers the
observed delivery contract, so the overlapping event adds no second refresh.
Under exactly the `CODEX_HOOK_EVENTS` registration, with `PostCompact` only
observed, a worker's three compactions each showed `PostCompact` firing and the
following compact `SessionStart` carrying one refocus block; the run held four
blocks in total, the first prompt's and one per compaction (§10).

`Notification` has no codex hook event. Codex instead has a fixed `notify`
program invoked with `agent-turn-complete` JSON as `argv[1]` and stdin closed;
`hooks/targets/notify_shim.py` bridges it into the ordinary `Notification`
handler.

### §2.2 Registration

`~/.codex/hooks.json`, with `[features] hooks = true` in `config.toml` — without
that flag the whole hook layer is dead weight. Events map to groups of command
hooks pointing at `~/.codex/agentihooks-hook.sh`, which sets
`AGENTIHOOKS_TARGET=codex` and execs `python -m hooks`.

Codex trusts hooks **by content hash**. Until trusted, it SILENTLY skips them —
run `/hooks` once inside a codex session, or launch automation with
`--dangerously-bypass-hook-trust`. `init` leaves an unchanged `hooks.json`
untouched and prints the trust advice only when it rewrites the file.

### §2.3 stdout contract

Codex parses hook stdout as exactly one JSON object, unlike Claude Code which
concatenates every raw stdout line. Handlers buffer through
`hooks/targets/emitter.py` and flush once at process exit. The predicate is
`buffers_single_envelope()` — copilot shares the property, so it is a target
capability rather than a codex identity check.

### §2.4 Permission channel

| | claude | codex | copilot |
|---|---|---|---|
| `permissionDecision` | allow/deny/ask | **deny only** | allow/deny/ask |
| `additionalContext` size before spilling | uncapped | **~2,500 tokens unless `additionalContextLimit` says otherwise** | uncapped |
| `additionalContext` on PreToolUse | yes | **no** | yes |
| `modifiedArgs` | no | no | yes |

Encoded in `hooks/targets/capabilities.py`. Blocking via exit code 2 + stderr
works on every codex event, same as Claude — which is why
`requires_envelope_block("codex")` is False.

### §2.5 Payload normalization

Codex cloned Claude Code's hook stdin contract almost verbatim, and 0.154.0
goes further: it translates its own shell tool into Claude's vocabulary at the
hook boundary. A turn the rollout records as `custom_tool_call name=exec`
arrives at the hook as `tool_name: "Bash"`, `tool_input: {"command": "<string>"}`
(captured live, 2026-09-11). Reading the rollout alone suggests the opposite and
is how this was misread once already.

The patch tool is the exception and was a real hole: `apply_patch` arrives under
its own name with the whole patch body in `command`, so it reached neither the
Bash branch nor the Write/Edit one — a secret written through a patch was never
scanned. `_CODEX_TOOL_NAMES` in `hooks/targets/normalizer.py` maps it to `Edit`,
aliases the body into `content`/`new_string` and lifts the target path out of
`*** Update File:` into `file_path`. The shell names (`exec`, `shell`,
`local_shell`, `unified_exec`) are mapped defensively in case a version stops
translating, along with a list-shaped `command` (0.147) and a code-mode freeform
`input` program.

The question tool arrives as `request_user_input` with a Claude-shaped
`{"questions": [...]}` input and is mapped to `AskUserQuestion`, so the
operator-away refusal covers Codex. 0.160.0 offers it in Plan mode, and in
Default mode only with the `default_mode_request_user_input` feature on; the
hook sees it and an exit 2 block refuses it in both (verified live, 2026-10-06).
Its `tool_response` is a JSON string, `{"answers": {<id>: {"answers": [...]}}}`;
`normalize_payload` turns it into Claude's `{"answers": {<id>: "a, b"}}`, so the
operator words recorder and the release, branch and PR signal detection read a
Codex answer like a Claude one.

0.154.0 also sends `transcript_path`, so `codex_rollout_path()` is now a
fallback rather than the only source.

Where `transcript_path` is absent, `codex_rollout_path()` resolves it from the
session id — rollouts live at
`<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<stamp>-<session id>.jsonl` — and only
for the transcript-driven events (`SessionEnd`, `Stop`, `SubagentStop`,
`PreCompact`), so a filesystem lookup is not charged to every tool call.

## §3 Feature-kind mapping

| row | Bundle kind | Codex behaviour |
|---|---|---|
| 14 | `skills` | global: symlinked into `~/.agents/skills` (open agent-skills standard dir, not under `.codex`). Repo scope is `<repo>/.agents/skills`, which the project bridge points at `.claude/skills` |
| 15 | `commands` | global: not installed; codex-cli 0.160.0 no longer reads `~/.codex/prompts` (live probe: `Unrecognized command '/prompts:<name>'`), and prompts an earlier install wrote are removed. Profile homes: hardlinked as skills (below) |
| 16 | `agents` | **skipped** — codex has no custom-subagent registry |
| 17 | `rules` (global) | no auto-loaded rules dir; compiled into `AGENTS.md` |
| 18 | `rules` (repo) | no loader at all; `hooks/context/project_bridge.py` injects every `<repo>/.claude/rules/*.md` body plus the project `MEMORY.md` at SessionStart |

## §4 Target abstraction

`TargetAdapter` in `scripts/targets/__init__.py`: `home`, `write_settings`,
`install_features`, `install_persona`, `register_hooks_utils`, `register_mcp`,
`post_install_reconcile`, plus a `doctor()` convention.

`resolve_target()` precedence: `--target` flag → `AGENTIHOOKS_TARGET` env →
exactly-one-installed-target recall → interactive TTY prompt → non-interactive
multi-target warning + default → `DEFAULT_TARGET` (`claude`).

Target-neutral helpers shared with the copilot adapter live in
`scripts/targets/_common.py` — notably `build_persona()` and `write_persona()`,
so the identity preamble and the operator-tail preservation rules cannot drift
between targets.

## §5 Install surface

| Concern | Codex path |
|---|---|
| Config home | `~/.codex` (`CODEX_HOME` overrides; first entry of a comma list) |
| Settings | `~/.codex/config.toml`, managed keys only |
| Hooks | `~/.codex/hooks.json` + `~/.codex/agentihooks-hook.sh` |
| Persona | `~/.codex/AGENTS.md` |
| Skills | `~/.agents/skills/` (repo scope: `<repo>/.agents/skills`) |
| Project doc | `<repo>/AGENTS.md`, or `<repo>/CLAUDE.md` via `project_doc_fallback_filenames` |
| MCP | `[mcp_servers.*]` tables in `config.toml` |
| Transcript | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |

### Profile homes

`agentihooks select-profile NAME --agent codex` renders
`~/.agentihooks/profiles/NAME/codex/` and sets `CODEX_HOME` to it. The rendered
Claude profile is the source; the Codex home is links into it and back to the
operator home:

| Entry | Points at |
|---|---|
| `AGENTS.md` | `../claude/CLAUDE.md` (persona and rules) |
| `skills/<name>` | `../claude/skills/<name>` |
| `skills/<replacement>` | the Codex replacement of a Claude plugin the chain enables: the installed plugin's own skills, a skill fetched into `~/.agentihooks/codex-skills/`, or a chain layer's `.codex/skills/<name>`. A Claude skill of the same name wins over a plugin or fetched skill; a chain layer skill wins over both |
| `skills/<command>/SKILL.md` | hardlink to each Claude command with frontmatter, invoked as `$<command>`; a skill of the same name wins |
| `auth.json`, `sessions`, `history.jsonl`, `session_index.jsonl`, `hooks.json` | the operator `~/.codex` |
| `config.toml` | generated: the profile's native settings, `sqlite_home` = operator home, the operator's hook trust rekeyed to this home's `hooks.json`, the operator's `[mcp_servers]` limited to the profile's servers, and `~/.agents/skills` switched off |

Codex keys hook trust by the hooks file path and a content hash; the hash does
not depend on the path (codex-cli 0.160.0, live probe), so rekeyed trust holds.
The key also carries each hook's position (`hooks.json:session_start:0:0`), so a
moved group loses its trust and Codex stops at the hooks review screen before its
first turn. `init` writes the agentihooks group first in every event; tools that
append their own group, such as herdr, keep the positions the operator trusted.
A command reaches Codex as a hardlinked `SKILL.md`: codex-cli 0.160.0 skips a
symlinked `SKILL.md` and refuses one without `---` frontmatter (live probe). The old `~/.codex/NAME.config.toml` files
are removed when their profile renders.

`config.toml` is written through a **tomlkit round-trip** so operator hand-edits
outside the managed key set survive every re-init — the TOML analogue of
`_preserve_personal_keys`. Managed values are recorded under
`[agentihooks.managed]`; a key that differs from that record was hand-edited and
is left alone with a warning.

**Permission translation.** `permissions.defaultMode: bypassPermissions` →
`approval_policy = "never"`, `sandbox_mode = "danger-full-access"`; anything else
→ `"on-request"` / `"workspace-write"`.

**Plugins.** Codex has no Claude plugins. A profile whose own chain layers enable
a plugin with no Codex replacement is Claude only, and the swarm never spawns it
on Codex; each replacement and the swarm rule are listed in
[the swarm pillar](../pillars/swarm.md#templates). The replacement paths are part
of the render stamp, so a fetch that adds one re-renders the home.

**Planner sandbox.** A swarm planner on Codex launches with
`-c permissions.planner={extends=":read-only", network={enabled=true}, filesystem={…}}`
and `-c default_permissions="planner"`. The filesystem grants write the ledger
folder, `~/.agentihooks/swarm` and `~/scratchpad` only, so the repository stays read
only while `agentihooks ledger` (which locks a file in the ledger folder) and
Redis calls succeed. This overrides the role's `sandbox_mode = "danger-full-access"`
for that launch only; every other role keeps it.

**Degrades.** Codex has no command-backed statusline (upstream openai/codex
#20140), so `tui.status_line` gets the closest built-in items and the `ah:`
profile line is emitted as a SessionStart banner instead.
Status items carry fixed labels: `used-tokens` renders `<N> used`, the session's
cumulative total, so the line pairs `context-used` with `context-window-size` and
leaves the cumulative item out (codex-cli 0.160.0, `StatusLineItem`).

**Project bridge.** `hooks/context/project_bridge.py` runs at SessionStart
(flag `PROJECT_BRIDGE_ENABLED`, budget `PROJECT_BRIDGE_MAX_BYTES`, 0 = whole).
It is the only part of agentihooks that writes **inside a repo**, and it writes
exactly two things: the symlink `<repo>/.agents/skills -> .claude/skills`, and a
`/.agents/` line in `<repo>/.git/info/exclude` (never `.gitignore`, which is
committed). An existing non-symlink at that path is left alone. The exclude goes
into the **common** git dir so a linked worktree writes where git reads. The
rules and project memory it injects ride the ordinary `inject_context` path, so
they join the single stdout envelope.

**Context spilling.** Every handler in `hooks.json` carries
`additionalContextLimit: 0`. Unset, codex spills any `additionalContext` over
~2,500 tokens to disk and hands the model a preview plus recovery metadata —
which silently truncated every agentihooks injection larger than that.

**Persona ceiling.** Codex caps the combined instruction doc at
`project_doc_max_bytes` (default 32 KiB). 0.147.0 loaded a 415 KB global
`AGENTS.md` in full, but the adapter raises the ceiling defensively anyway — a
silently truncated persona is the worst failure mode the file can have.

## §6 Transcript format

Rollout JSONL. Top-level `type` is one of `session_meta`, `response_item`,
`event_msg`, `turn_context`, `world_state` — the discriminator
`detect_transcript_format()` uses.

`response_item` carries messages and tool calls; `event_msg` carries
`token_count` and `task_complete`. `task_complete` repeats the turn's last agent
message, so it is emitted as `turn_complete` **only** when the turn produced no
`assistant_text` — emitting both double-counts every turn.

Codex outputs carry **no error flag**, so `is_error` is always False and codex
sessions rely on the live PostToolUse recording path rather than transcript error
scanning.

## §7 MCP registration

`[mcp_servers.<name>]` tables in `config.toml`:

- stdio → `{command, args, env}`
- http → `{url, http_headers}`, codex infers the type from the url alone

**SSE is skipped with a warning** — codex has no SSE client. Expose a
streamable-HTTP endpoint and re-run init. (Copilot diverges here: it ships an SSE
client, so its adapter must not copy this branch.)

**Header placeholders.** Claude Code expands `${VAR}` in header values at connect
time; codex sends them **literally** (verified: gateway 401 on the raw
placeholder). `Authorization: Bearer ${VAR}` maps to codex's native
`bearer_token_env_var`; any other placeholder-bearing header is dropped with a
warning.

Every env and header value is scanned by `hooks.secrets.scan` before being
written to disk — a HARD FLOOR path.

## §8 Known gaps

- **Uninstall.** `uninstall_global()` removes Claude artifacts only; nothing
  tears down `~/.codex`. Tracked in the defer log.
- **Hook trust** cannot be verified from outside codex — `doctor()` says so
  rather than implying a green check covers it.
- **Agents** have no codex equivalent (§3 row 16).
- **`Interrupt`** exists in codex's `HooksToml` schema and is not in
  `CODEX_HOOK_EVENTS`; it has no `hook_manager` handler today.
- **`PostCompact`** fires in codex-cli 0.160.0. It remains unregistered because
  the subsequent compact `SessionStart` restores swarm intent (§2.1).

## §9 Verification

```bash
agentihooks init --target codex
agentihooks doctor --target codex
./scripts/codex_smoke.sh              # live, against real `codex exec` turns
uv run python -m pytest tests/test_codex_target.py tests/test_codex_e2e.py tests/test_project_bridge.py
```

## §10 Evidence

| Claim | Established by |
|---|---|
| Automatic compaction events and master refocus | codex-cli 0.160.0, lowered `model_auto_compact_token_limit` scratch run; captured PreCompact → PostCompact → SessionStart compact; QA payload replay controls in `tests/context/test_swarm_refocus.py` |
| `PostCompact` unregistered, refocus once per compaction | codex-cli 0.160.0, 2026-10-07, dev 0a2fb970; Codex home registering exactly `CODEX_HOOK_EVENTS` through `python -m hooks` plus an observe-only `PostCompact` logger; worker on a swarm task, three native `compacted` records, three `PostCompact` firings with no handler, one refocus block on each compact `SessionStart` and four in the run |
| Hook events, `hooks.json` shape, content-hash trust | codex-cli 0.147.0, 2026-08-10; encoded in `CODEX_HOOK_EVENTS` and asserted by `tests/test_codex_target.py::TestHooksJson` |
| One-JSON-object stdout contract | reproduced pre-fix as a two-line stdout; regression-guarded by `tests/test_codex_e2e.py` |
| PreToolUse deny-only, no context channel | codex-cli 0.147.0; encoded in `hooks/targets/capabilities.py` |
| SSE unsupported | codex-cli 0.147.0; `register_mcp` skip branch |
| Header `${VAR}` sent literally | observed gateway 401 on the raw placeholder |
| `project_doc_max_bytes` behaviour | 0.147.0 loaded a 415 KB `AGENTS.md` in full |
| Rollout path and record types | `codex_rollout_path()` + `tests/fixtures/codex_rollout_sample.jsonl` |
| Hook-boundary tool names and arg shapes | codex-cli 0.154.0, 2026-09-11: an isolated `CODEX_HOME` whose `hooks.json` captured raw stdin over one `codex exec` turn — `Bash` + `{"command": "<string>"}` for the shell tool, `apply_patch` + the patch body in `command` |
| `additionalContextLimit` default of ~2,500 tokens | codex's published `config-schema.json`, `$defs.HookHandlerConfig` |
| `project_doc_fallback_filenames` loading a repo CLAUDE.md | `codex -c 'project_doc_fallback_filenames=["CLAUDE.md"]' debug prompt-input` in tcc-qitp: instructions block 36,998 → 77,358 chars |
| Repo skill root `<repo>/.agents/skills` | 0.154.0 binary strings ("failed to stat repo skills root" beside ".agents"); global roots confirmed live via `debug prompt-input` |
| What Claude loads that codex did not | a real tcc-qitp session transcript: one `instructions` attachment, 37 files, 252,291 bytes, every rule body inline |
| Everything in §3–§5 not listed above | code-derived from `scripts/targets/codex_target.py` during this reconstruction; not re-verified against a codex binary |

## Native settings authoring (v2.3+)

Profiles author codex settings in codex's own TOML at
`<profile>/.codex/config.overrides.toml`, merged over
`profiles/_base/config.base.toml`. Nothing is translated from Claude settings
any more — the previous design could carry only `permissions.defaultMode` and
hardcoded the rest in Python, so `model_reasoning_effort` and every other
codex-native key were unreachable from a bundle.

Merge discipline: each top-level key is applied under the
`[agentihooks.managed]` record, so a value the operator hand-edited since our
last write is left alone. Nested tables (`tui`, `features`, …) merge key by key
rather than being replaced wholesale — `config.toml` is a shared operator file
and replacing a table would silently drop settings we never wrote.
`features.hooks = true` is re-applied as a floor after the merge: the hook layer
is the entire guardrail surface and is never left off.

`[mcp_servers.*]` is not written from this file; MCP continues through
`register_mcp`.

### Official schema — prefer it over prose

OpenAI publishes the serde-generated JSON Schema for `ConfigToml`:

```
https://developers.openai.com/codex/config-schema.json
```

Put `#:schema https://developers.openai.com/codex/config-schema.json` at the top
of a config.toml for editor completion. Because it is generated from the Rust
struct it cannot drift the way hand-written docs can, so it is the authority
when the two disagree. `codex --strict-config <cmd>` errors on any key the
installed binary does not recognise — a cheap CI check for a hand-authored file
against an exact version.

### Posture keys worth stating explicitly

| key | values |
|---|---|
| `approval_policy` | `untrusted`, `on-request`, `never`, or a `granular` table |
| `sandbox_mode` | `read-only`, `workspace-write`, `danger-full-access` |
| `model_reasoning_effort` | `minimal`, `low`, `medium`, `high`, `xhigh` |

`sandbox_workspace_write.{writable_roots,network_access}` refine
`workspace-write`. The base ships the safe pairing
(`on-request` / `workspace-write`); a profile wanting autonomy states
`never` / `danger-full-access` itself rather than having it inferred.

### Credential protection

codex has no path-rule permission mechanism, so the Claude `permissions.deny`
rules have no faithful codex equivalent and are deliberately NOT approximated
here — a rule that reads as protection it does not provide is worse than none.
Credential-read protection comes from the shared hook layer
(`hooks/context/credential_guard.py`), which runs on every target.

### Machine-managed — never hand-write

`[hooks.state]` (hook trust hashes), `tui.model_availability_nux.*`, and
`[projects.<path>].trust_level` are written by codex itself.
