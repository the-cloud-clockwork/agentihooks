# COPILOT-COMPAT — GitHub Copilot CLI as an AgentiHooks target

Reference for the `copilot` install target. Every claim carries how it was
established, so a reader can re-derive it against a newer CLI rather than
trusting this page.

**Verified against `@github/copilot` 1.0.79-6, re-verified in part against
1.0.83 (2026-09-11).** Copilot ships several releases per week, has removed a
flag pair (`--headless --stdio`) with no deprecation window, and `autoUpdate`
defaults to true — it upgraded itself 1.0.80 → 1.0.83 mid-probe while this page
was being written. Every claim here is true of a version, not of "copilot":
re-run `agentihooks doctor --target copilot` and `scripts/copilot_smoke.sh`
after an upgrade rather than trusting the prose.

## §1 What a target is

See `scripts/targets/__init__.py`. A target is the agent CLI whose config
surface `agentihooks init` writes. Profile resolution, bundle linking, settings
merging and MCP server-dict assembly are target-agnostic; everything touching a
target-specific path or schema goes through the adapter from `get_adapter()`.

## §2 Hook contract

### §2.1 Events

Copilot's `HookType` enum carries 17 events. AgentiHooks wires the 12 that
`hook_manager.EVENT_HANDLERS` dispatches, registered under the **enum
spellings** from `schemas/api.schema.json`:

`sessionStart`, `sessionEnd`, `userPromptSubmitted`, `preToolUse`,
`postToolUse`, `postToolUseFailure`, `agentStop`, `subagentStart`,
`subagentStop`, `preCompact`, `permissionRequest`, `notification`.

⚠️ Two of these are **different tokens**, not case variants: Claude's `Stop` is
Copilot's `agentStop`, and `UserPromptSubmit` is `userPromptSubmitted`.
Lowercasing a Claude event name produces an event that does not exist and is
silently ignored by the loader.

Copilot's loader also accepts the Claude-style PascalCase aliases — verified on
1.0.80 (see the probe below) — but the enum spellings are what gets registered:
they carry direct schema evidence, and loader acceptance is not by itself proof
that an alias reaches the same handler.

Not registered: `preMcpToolCall`, `userPromptTransformed`, `errorOccurred` —
no distinct handler exists, but the normalizer maps each onto the nearest
dispatched event, so one arriving anyway is handled rather than dropped.

`postResult` and `prePRDescription` are neither registered **nor** mapped:
folding `postResult` onto `Stop` would re-run session-end work once per agent
result, and `prePRDescription` has no analogue. If either arrives, hook_manager
logs it as an unknown event.

Both spellings resolve. Registration uses the PascalCase aliases; Copilot may
echo either those or its own camelCase, and an unmapped name reaches no handler
and exits 0 — a silent bypass of every guardrail for that event. `_COPILOT_EVENTS`
therefore carries a camelCase entry, a derived identity entry per dispatch name,
and an explicit entry for `PostToolUseFailure` (registered PascalCase but
dispatched as `PostToolUse`, so the derived pass cannot cover it).
`tests/test_hook_targets.py::test_registered_pascalcase_spelling_resolves`
asserts every registered event resolves to a real handler.

Unlike codex, Copilot has native `PostToolUseFailure` and `Notification` events,
so **no `notify_shim` equivalent is needed**.

### §2.2 Registration

`~/.copilot/hooks/agentihooks.json`:

```json
{ "version": 1,
  "hooks": { "SessionStart": [ { "type": "command",
                                 "command": "~/.copilot/agentihooks-hook.sh",
                                 "timeoutSeconds": 30 } ] } }
```

Copilot merges hook definitions from several sources — admin policy files,
`.github/hooks/*.json`, `$COPILOT_HOME/hooks/`, an inline `hooks` key in
`settings.json`, and plugins. AgentiHooks writes the **hooks directory only**.
Writing both the directory file and the inline settings key fires every hook
twice.

Trust is keyed by content hash via the `disabledHooks` setting, so editing the
wrapper invalidates the hash and needs re-approval.

**Verified empirically against 1.0.80.** The loader runs *before* auth, so this
is testable without a Copilot subscription. Write a hooks file containing every
event name plus one deliberately bogus canary, point `COPILOT_HOME` at a scratch
dir, and run `copilot -p hi --log-level all --log-dir <dir>`:

```
Ignoring unknown hook event(s) in <path>/hooks/agentihooks.json: ZZZ_CANARY_NOT_REAL
```

Only the canary is named — for both the camelCase enum spellings and the
PascalCase aliases. That is what proves the names are recognised, that the
loader really validates (it rejects the canary rather than accepting anything),
and that `$COPILOT_HOME/hooks/*.json` is read at all, since the message names
the file. `scripts/copilot_smoke.sh` [2b] runs this check with the canary as a
second assertion, so a loader that silently ignored everything cannot score a
pass.

A grep of the binary for a quoted `"SessionStart"` returns nothing, which looks
like proof the aliases are rejected — it is a false negative. The alias
resolution is not a table of literal strings, and the `config.json` →
`settings.json` migration copies keys verbatim without normalising them, so
neither observation speaks to what the loader accepts. Only the probe does.

Two caveats from the same probe: the loader **tolerates unrecognized keys
silently** (a deliberately bogus key produced no complaint), so "no error" is
not evidence a field is honored; and the timeout field is written as
`timeoutSeconds`, because `timeoutSec` — the spelling in the public hooks
reference — appears zero times in `app.js` while `timeoutSeconds` appears in
both `app.js` and the native engine. A serde symbol dump of the engine does show
`timeoutSec` adjacent to the `HookConfig` discriminants, so the two spellings may
both be live on different paths; `timeoutSeconds` is the one with evidence in
both layers. Copilot fails OPEN on timeout, so the wrong spelling degrades to
the default rather than to a block.

`HookConfig` also carries `matcher` (a regex over tool names) and
`allowedEnvVars`, neither of which the adapter sets — unused capability, not a
gap.

### §2.3 stdin contract — SETTLED LIVE (v1.0.80, 2026-08-19)

Real payloads, captured from authenticated sessions (`session-state/<sid>/
events.jsonl`, `hook.start` records):

- **No event-name field at all.** Stdin is the event's input object alone —
  no `hookEventName`, no `hookType`. The registration is the event's
  identity, so the adapter registers each event with its name as argv[1]
  (`agentihooks-hook.sh preToolUse`); the wrapper exports it as
  `AGENTIHOOKS_COPILOT_EVENT` and the normalizer falls back to it. Hook
  commands run through `/bin/sh` (binary: "`/bin/sh` on Unix-like systems,
  `cmd.exe` on Windows"), so the argument survives the spawn.
- **preToolUse** sends a batched array, args stringified:
  `{"sessionId", "cwd", "toolCalls": [{"id", "name", "args": "<json string>"}]}`.
  NOT `toolName`/`toolArgs`. Tool names are copilot's runtime vocabulary at
  the hook boundary: `bash` (not `shell`), `view`, `create`, `edit`, `grep`,
  `glob`. Arg keys: `path`, `file_text` (create), `old_str`/`new_str` (edit),
  `command` (bash). Two more builtin WRITE tools exist that are NOT single
  verbs and were a HARD FLOOR bypass until mapped: `apply_patch` carries a
  whole diff in `{patch}` (aliased into `content` so the scan reads it), and
  `str_replace_editor` is a dispatcher whose real verb is a nested `command`
  (`create`→Write, `str_replace`/`insert`→Edit, `view`→Read). Both are in
  copilot's own write set `new Set(["apply_patch","create","edit","str_replace"])`.
  The normalizer maps names and arg keys onto the Claude
  spellings the guardrails read, and `hook_manager` runs every batched call
  through the PreToolUse pipeline — a deny from any call denies the whole
  batch via exit 2 (the safe over-block; per-call stdout denial exists in the
  wire format but the command-hook shape was not confirmed live).
- **postToolUse** sends singular `toolName` + `toolArgs` (STILL a JSON
  string) + `toolResult: {resultType, textResultForLlm, ...}`.
- Other observed fields: `timestamp` (ms epoch), `cwd`; `agentStop` carries
  `transcriptPath` (the events.jsonl) and snake_case `stop_hook_active`;
  `sessionEnd` carries `reason`.

### §2.4 stdout contract — SETTLED LIVE

One JSON object, top-level fields only — established by LIVE probe, which is
what governs here. The field names `hookSpecificOutput`, `permissionDecision`,
`permissionDecisionReason`, `modifiedArgs`, `updatedInput` DO appear in
`runtime.node` (in one `HookConfig` internally-tagged-enum string pool
alongside `allowedEnvVars`/`vsCodeCompat`/`suppressOutput`), and `app.js`'s
`hookProcessorPreToolUse` consumes `argMutations`/`additionalContexts`/
`denials`/`askRequests` per tool-call. So the *native/aggregated* hook-result
shape is richer than what a command hook can drive. But for the **command**
transport this integration uses, the live v1.0.80 binary honored only the
top-level shape: a nested `hookSpecificOutput.additionalContext` canary never
reached the model while a top-level `additionalContext` did, and a preToolUse
`{"decision":"block"}` on stdout did not deny (the tool ran) while exit 2 did.
Treat the struct pool as the union across transports, not as the command-hook
stdout contract.

| Field | Effect |
|---|---|
| `additionalContext` (top-level string) | injected into model context (arrives as a `user.message` with `source: "system"`) |
| `{"decision": "block", "reason": ...}` | blocks `userPromptSubmitted` ("Prompt blocked by hook: <reason>", zero model credits). Ignored on `preToolUse`. |

`emitter.flush()` emits the top-level shape on copilot; `emit_permission_decision`
emits `{"decision": "block", "reason"}` there. Codex/claude keep the nested
`hookSpecificOutput` shapes. `modifiedArgs`/allow/ask are in the runtime's
result union but were not driven from a command hook's stdout in the live
probe; `supports_arg_mutation()` stays a declared, unconsumed seam. A
consequence for batching: this integration denies via exit 2, which denies the
whole preToolUse batch — the safe over-block. The wire format's per-tool-call
`denials[{toolCallId}]` could deny one call and pass the rest, but the
command-hook stdout shape that populates it was not confirmed live, so the
conservative batch-wide deny stands.

### §2.5 Exit-code semantics — SETTLED LIVE

| Event | exit 2 | stdout `{"decision":"block"}` |
|---|---|---|
| `preToolUse` | **denies** ("Denied by preToolUse hook: hook exited with code 2") | ignored (tool ran) |
| `userPromptSubmitted` | advisory (turn ran to completion) | **blocks** (no model call) |

Both cells were asserted against the live binary — the probes are automated in
`scripts/copilot_smoke.sh` §[8]. Consequence: `requires_envelope_block()`
returns true for copilot on `PreToolUse`, `PermissionRequest` AND
`UserPromptSubmit` — on the last one the envelope is the ONLY block channel.

**Timeouts always fail OPEN**, on every event including `preToolUse`. A hung
hook does not block — it stops guarding. This is why `timeoutSeconds` is set
generously (30s) rather than tightly.

## §3 Surface map

| Concern | Copilot path | Notes |
|---|---|---|
| Config home | `~/.copilot` | `COPILOT_HOME` overrides; no XDG support |
| Settings | `~/.copilot/settings.json` | plus `settings.local.json`; repo scope `.github/copilot/settings.json` |
| Machine config | `~/.copilot/config.json` | **never written** — CLI-managed, its own header says "User settings belong in settings.json" |
| Hooks | `~/.copilot/hooks/*.json` | repo scope `.github/hooks/*.json` |
| Persona | `~/.copilot/copilot-instructions.md` | see the instruction list below |
| Project doc | `<repo>/CLAUDE.md` | **native, no config needed** — unlike codex, which needs `project_doc_fallback_filenames` |
| Project rules | — | no loader; `hooks/context/project_bridge.py` injects `<repo>/.claude/rules/*.md` + the project `MEMORY.md` at SessionStart |
| Instructions | `~/.copilot/instructions/**/*.instructions.md` | path-scoped; not used by agentihooks |
| MCP | `~/.copilot/mcp-config.json` | key `mcpServers`; repo scope `.mcp.json` / `.github/mcp.json` |
| Skills | `~/.copilot/skills/`, `~/.agents/skills/` | agentihooks uses the open standard dir, shared with codex. Repo scope: `.github/skills/`, `.agents/skills/`, **`.claude/skills/`** — so a Claude-shaped repo's skills load with no link, and `needs_repo_skills_link()` is False for copilot |
| Agents | `~/.copilot/agents/*.md` | MD + YAML frontmatter. The loader filters on `endsWith(".md")`, so our `<name>.md` loads; the CLI's own create path writes `<name>.agent.md`, which is the same filter |
| Commands | — | no prompt-file mechanism (github/copilot-cli#1113) |
| Status line | `settings.json` `statusLine` | `type: "command"`, session JSON on stdin |
| Session log | `~/.copilot/session-state/<id>/events.jsonl` | see §6 |

### §3.1 Instruction files the CLI loads by itself

From the `customInstructions` table in the shipped `app.js` (1.0.83), scope note
"in git root & cwd":

| Scope | Paths |
|---|---|
| Project | `CLAUDE.md`, `GEMINI.md`, `AGENTS.md`, `.github/instructions/**/*.instructions.md`, `.github/copilot-instructions.md` |
| Personal | `$HOME/.copilot/copilot-instructions.md`, `$HOME/.copilot/instructions/**/*.instructions.md` |
| Extra | `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` — comma-separated additional **directories**, searched for those same filenames |

`--no-custom-instructions` disables all of it. `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`
is not a codex-style `project_doc_fallback_filenames`: it adds directories, not
filenames, so it cannot make `.claude/rules/*.md` load — which is why the bridge
injects them instead.

## §4 What the adapter writes

`scripts/targets/copilot_target.py`.

**settings.json** — managed keys only, recorded under an `agentihooks.managed`
key so a value the operator hand-edited since the last write is left alone and
reported. `statusLine` wires to `python -m hooks.statusline`; `disableAllHooks`
is pinned false because an inherited true kills every guardrail silently.

`trustedFolders` is deliberately **outside** the managed-key rule. It is a set
the operator also edits via `/add-dir`, so treating a non-empty list as a
hand-edit would mean never seeding the repo root on any machine that had ever
trusted a directory. It is unioned, never replaced.

**Features** — skills symlink to `~/.agents/skills`; agents translate into
`~/.copilot/agents`; commands translate into skills (see §5); rules compile into
the persona, since Copilot auto-loads instruction files rather than a rules dir.

**Persona** — `copilot-instructions.md`, assembled by the shared
`scripts/targets/_common.build_persona()`: identity preamble → bundle CLAUDE.md
→ profile-chain CLAUDE.mds → compiled rules → CI manifesto, between
`_MANAGED_HEADER` / `_MANAGED_FOOTER` so an operator-appended tail survives
re-init. Copilot has no `project_doc_max_bytes` analogue, so no ceiling is
raised.

## §5 Divergences from the codex adapter

| | codex | copilot | why |
|---|---|---|---|
| Agents | skipped | installed | Copilot has a real custom-agent registry |
| Commands | `~/.codex/prompts/` | skills in `~/.agents/skills/` | Copilot has no prompt-file mechanism |
| SSE MCP | dropped with a warning | supported | Copilot ships an SSE client |
| Header `${VAR}` | mapped to `bearer_token_env_var` | dropped with a warning | Copilot has no env-indirection field and sends header values literally |
| Status line | static `tui.status_line` items | command-backed | Copilot supports a command status line |
| Notification | `notify` shim bridge | native event | Copilot has a `notification` hook |
| Config format | TOML via tomlkit | JSON | — |

**Command translation.** Each `commands/<name>.md` becomes
`~/.agents/skills/<name>/SKILL.md` with `name` + `description` frontmatter. It
loses `/name` invocation and becomes model-discoverable instead.

**Shared-directory hazard.** `~/.agents/skills` holds both codex's symlinks and
copilot's translated commands. The translation manifest is a separate file
(`.agentihooks-copilot-commands.json`, not the `.agentihooks-manifest.json`
codex uses for prompts), reaping only real directories it wrote and never
following or deleting a symlink. A symlinked skill of the same name always wins
over a translated command.

**Agent frontmatter translation.** Claude tool names map to Copilot runtime
names: `Read`→`view`, `Write`→`create`, `Edit`→`edit`, `Bash`→`shell`,
`Grep`→`grep`, `Glob`→`glob`, `WebFetch`→`web_fetch`, `WebSearch`→`web_search`,
`Agent`/`Task`→`task`, `TodoWrite`→`update_todo`,
`AskUserQuestion`→`ask_user`. Scoped grants (`Bash(git diff*)`) reduce to the
bare mapped tool — copilot's grammar has no scoping. A claude `model` alias
(`haiku`, `sonnet`) is dropped: it is not a copilot model id and produces a
per-invocation "model not available" warning before falling back to auto,
which dropping the field does silently. `description` is required by Copilot
and is synthesized when the source omits it, or the agent is unloadable.
Bodies over 30,000 characters are truncated at a paragraph boundary with a
marker. Note the naming split: `shell` is the frontmatter grant name (accepted
without warnings, grants applied — verified live via `copilot --agent`), while
the hook boundary reports the same tool as `bash`.

**Settings managed-key record.** Lives in the `~/.copilot/.agentihooks-managed.json`
sidecar, NOT in settings.json — copilot warns "Ignoring unknown top-level
key(s)" on every launch for any key it does not recognize. Legacy in-file
`agentihooks` keys migrate to the sidecar on the next init.

**Auth portability.** Credentials ride in `~/.copilot/config.json`: copying
that file into a scratch `COPILOT_HOME` authenticates it (proven live; the
smoke script's live tier uses exactly this, never reading the file).

## §6 Transcript format

`~/.copilot/session-state/<session-id>/events.jsonl`.

VERIFIED against live captures (2026-08-19, v1.0.80, authenticated
sessions): the filename, the dotted-type union, `hook.start`/`hook.end`
records (which carry the `hookType` and the exact stdin `input` object — the
source of every §2.3 fact), and the reader's output (tool call/result
correlation, metrics). `tests/fixtures/copilot_events_real.jsonl` is one such
capture, with the system prompt elided and the synthetic test key scrubbed;
`copilot_events_sample.jsonl` remains the schema-derived edge-case fixture
(it exercises a failed tool, which the capture lacks).

One capture-only fact the schema does not state: hook-injected
`additionalContext` arrives back as a `user.message` with `source: "system"`.
The reader classifies those as `system_text`, not `user_text` — counting them
as user turns would charge every injection as a turn (observed: 4 turns
instead of 1).

Each line is
`{id, timestamp, parentId, type, data}` where `type` is a dotted namespace
string. No other target writes a dotted type, so the prefix alone discriminates
the format (`hooks/memory/transcript_reader.py`).

Mapped into `TranscriptRecord`:

| Copilot event | record kind |
|---|---|
| `session.start` | `meta` |
| `user.message` | `user_text` |
| `assistant.message` | `assistant_text` |
| `system.message` | `system_text` |
| `tool.execution_start` | `tool_call` |
| `tool.execution_complete` | `tool_result` (`is_error` = `not data.success`) |
| `assistant.usage` | `token_usage` |
| `session.task_complete` | `turn_complete`, only when the turn produced no `assistant.message` |

The envelope also carries `ephemeral` and `agentId`, which the reader uses to
skip streaming deltas (§6 rules below) and ignores respectively.

Two rules that matter:

- **Ephemeral events are skipped** (except `assistant.usage`, which is marked
  ephemeral but is the only usage record). Streaming deltas and re-renders would
  otherwise multiply every reply by its chunk count.
- **`session.task_complete` is a fallback only.** Emitting it alongside
  `assistant.message` double-counts every turn for consumers that treat the two
  as equivalent digest text — the same rule the codex reader applies to
  `task_complete`.

Unlike codex, Copilot states tool success explicitly, so transcript error
scanning works and `tool_memory` does not have to fall back to live PostToolUse
recording.

## §7 Payload normalization

Copilot sends camelCase (`sessionId`, `toolName`, `toolArgs`, `toolResult`) and
its own event vocabulary. `hooks/targets/normalizer.py` fills both spellings and
maps every known event name onto the single vocabulary `EVENT_HANDLERS`
dispatches on, so a guardrail cannot depend on which spelling arrives. The
original name is preserved as `copilot_event_name` for diagnostics.

Copilot omits `transcript_path`; `copilot_events_path()` resolves it from the
session id for the transcript-driven events only.

## §8 Known gaps

- **Slash commands.** No prompt-file support upstream; commands are reachable as
  skills, not as `/name`.
- **`allow`/`ask`/`modifiedArgs` on preToolUse.** Documented upstream, not
  observed in the v1.0.80 binary (§2.4). If a later release ships them,
  `supports_arg_mutation()` and `allowed_permission_decisions()` are the seams.
- **`sessionStart` `additionalContext` is contested upstream.** GitHub's hooks
  reference calls the event fire-and-forget and three open issues
  (github/copilot-cli#2142, #2585, #2980) report the field being ignored. On
  1.0.83 it works: a `copilot -p` canary quoted three SessionStart-injected
  banners back verbatim, which is what the project bridge relies on. The smoke
  test's rule canary is the regression guard; the documented fallbacks are
  `userPromptTransformed` and the first `userPromptSubmitted`.
- **`postToolUse` `additionalContext` is capped at 10 KB** upstream (documented),
  with no notice when it cuts. `context_cap_bytes()` in
  `hooks/targets/capabilities.py` truncates with a visible marker instead.
- **`postResult` / `prePRDescription` / `userPromptTransformed`** hook events
  exist upstream; the adapter does not register the first two (folding them
  onto Stop would re-run session-end work) and only maps the third.

Closed since first written: uninstall (`teardown()` parity, all targets), MCP
url/command/args secret-scanning (`mcp_spec_credential_hits`), exit-code
semantics (§2.5, settled live), `events.jsonl` (§6, captured live).

## §9 Verification

```bash
agentihooks init --target copilot
agentihooks doctor --target copilot
./scripts/copilot_smoke.sh              # live, against a throwaway COPILOT_HOME
uv run python -m pytest tests/test_copilot_target.py tests/test_copilot_e2e.py
```

## §10 Evidence table

Everything above was derived from the shipped package, not from documentation
alone. Reproduce with:

| Claim | How to re-derive |
|---|---|
| 17 hook events, exact names | `jq '.definitions.HookType.enum' schemas/api.schema.json` in the `@github/copilot-<platform>` package |
| Hook settings catalogue, `disableAllHooks`, `disabledHooks` | `strings prebuilds/*/runtime.node \| grep -o '{"path":"[^"]*[Hh]ook[^"]*"[^}]*}'` |
| Exit-code / timeout strings | `strings prebuilds/*/runtime.node \| grep -E 'Hook command (failed\|timed out\|exited)'` |
| Config paths (`mcp-config.json`, `copilot-instructions.md`, `skills/`) | `grep -oE '\.copilot/[A-Za-z0-9/._*-]+' app.js \| sort -u` |
| Repo-scope paths (`.github/hooks/*.json`, `.github/copilot/settings.json`) | `grep -oE '\.github/[A-Za-z0-9/._*-]+' app.js \| sort -u` |
| Settings semantics ("User settings belong in settings.json", `hooks`, `statusLine`, `trustedFolders`) | the `{name:"config",summary:"Configuration Settings"` help topic in `app.js` |
| Instruction files read (`AGENTS.md`, `CLAUDE.md`, `copilot-instructions.md`) | `grep -oE '"[A-Za-z.-]*\.md"' app.js \| sort -u` |
| Session-event envelope + `data` shapes | `jq '.definitions.SessionEvent.anyOf' schemas/session-events.schema.json` |
| Subcommands and 89 flags | `copilot completion bash`, or the installed `~/.local/share/bash-completion/completions/copilot` |
| `COPILOT_HOME` and other env vars | `grep -oE 'COPILOT_[A-Z0-9_]+' app.js \| sort -u` |

Cross-checked against `docs.github.com/en/copilot/reference/hooks-reference`,
`.../custom-agents-configuration`, and the MCP/instructions how-to pages.

Live-session evidence (2026-08-19, v1.0.80, authenticated):

| Claim | How it was established |
|---|---|
| stdin has no event-name field; preToolUse sends `toolCalls[]` with stringified `args` | `hook.start` records in `session-state/<sid>/events.jsonl` (the `input` object IS the stdin) |
| exit 2 denies preToolUse; ignored on userPromptSubmitted | probe hooks in a scratch `COPILOT_HOME` — automated in `scripts/copilot_smoke.sh` §[8] |
| `{"decision":"block","reason"}` blocks userPromptSubmitted; ignored on preToolUse | same probes; block path in app.js: `Ywr(on)` → `Prompt blocked by hook` |
| only top-level `additionalContext` reaches the model | two-canary probe (top-level word echoed, nested word invisible) |
| command-hook stdout honors top-level `additionalContext` / `decision`, not the nested `hookSpecificOutput` shape | live canary probe (nested invisible, top-level reached the model); the struct field names exist in the runtime union but do not govern the command transport |
| injected context arrives as `user.message` with `source:"system"` | captured events.jsonl (`tests/fixtures/copilot_events_real.jsonl`) |
| auth rides in `config.json` | copy into scratch home → authenticated turn |
| hook commands run via `/bin/sh` | binary string: "`/bin/sh` on Unix-like systems, `cmd.exe` on Windows" |
| `shell` valid as agent grant name; hook boundary reports `bash` | `copilot --agent` run + captured `toolCalls` |

## §11 Native settings authoring (v2.3+)

Profiles author copilot settings in Copilot's own format at
`<profile>/.copilot/settings.overrides.json`, merged over
`profiles/_base/settings.base.copilot.json`. MCP is authored separately at
`<profile>/.copilot/mcp-config.overrides.json`. Nothing is translated from
Claude settings any more.

**`_agentihooks` is a reserved block, not a Copilot setting.** Copilot warns
about unknown top-level keys on every launch, so the installer consumes this
block and never writes it to disk. It carries directives Copilot has no settings
key for:

| directive | effect |
|---|---|
| `allowAll: true` | writes `COPILOT_ALLOW_ALL=true` to `~/.agentihooks/copilot.env`, which the installer's `agentienv` shell block auto-exports. The value must be the literal `true` — see §11.7 |

**Never declare a `hooks` key.** Copilot merges an inline settings `hooks` with
the `hooks/` directory, so declaring both fires every hook twice. The adapter
drops it with a warning. Hooks are written to `hooks/agentihooks.json`.

### §11.1 `permissions.*` is enterprise-only — do not author it here

The settings catalogue describes `permissions.allow/ask/deny` as
*"Enterprise-managed permission rules"*, and that is literal: rules written into
**user** settings are inert. Settled live on v1.0.80 with three runs differing
only in the rule, same scratch home:

| `permissions.deny` | result |
|---|---|
| absent (control) | file read |
| `read(probe-target.txt)` + glob form | file read anyway |
| bare `read`, `view`, plus both scoped forms | file read anyway |

A bare `read` deny would block every read if the engine were live at this scope.
It did not. Authoring credential rules here would produce a file that reads as
protection while providing none.

Credential protection on copilot therefore comes from the agentihooks hook layer
(`hooks/context/credential_guard.py`, called from `on_pre_tool_use`), which is
the only mechanism that actually executes on this target.

### §11.2 MCP: OAuth is opt-in, and `tools` takes exact names

`auth` and `oidc` default to **false** per server. Left at Copilot's default, a
401 from any http/sse server starts a browser authorization flow; under WSL that
launches a Windows browser with no session and the turn hangs with nothing to
click. A server that genuinely needs OAuth sets `auth: true` explicitly.

`tools` is an **exact-name allowlist — wildcards are not supported**. A pattern
like `litellm_tools-*` silently matches nothing and disables the server
entirely; the tool count reads 0 and no error is raised. Verified live.

This matters because of the static-context ceiling: `gateway-tools` alone ships
511 tool schemas, which puts static context at 121% of the window on copilot's
small auto-routed models and aborts every turn at 0 credits with
`compaction_static_context_blocked`. Attribution measured by elimination —
shrinking the 66KB persona to 30 bytes moved it only 121% → 109%, while
dropping the heavy MCP servers cleared it outright. The tools are the bulk, not
the persona.

Tool-search deferral (`toolSearch` + per-server `deferTools`) exists but is
gated off server-side for non-enterprise accounts, and forcing the flags true
via `enabledFeatureFlags` changed nothing (byte-identical schema count, no
`tool_search` tool). Treat the allowlist as the working lever.

### §11.3 MCP OAuth fires at startup — `COPILOT_DEBUG_BROWSER` is the only interception point

Copilot connects **every** configured MCP server when the session opens and
starts an OAuth flow on the first 401, opening one browser tab per server. Under
WSL that is a Windows browser with no session, and the turn parks there. Four
configured Microsoft-auth servers produce four tabs.

There is no defer-auth, lazy-connect, or `autoConnect: false` key — all three are
open upstream requests (copilot-cli #1938, #2026, #3462), and `deferTools` defers
only tool *schemas*, not the connection. `disabledMcpServers` (settings, written
by `/mcp disable`) stops the flow by stopping the server, which is the wrong
trade when the server is wanted.

`COPILOT_DEBUG_BROWSER` is checked ahead of every launch path — before the
remote-environment skip, before `$BROWSER`, before `xdg-open`. It takes a JSON
string array; Copilot spawns `array[0]` with the remaining elements plus the URL
appended. Pointing it at a sink keeps servers configured and connected while no
browser opens, and the authorization URL is recoverable:

```
_agentihooks: { "suppressBrowserLaunch": true }
```

The adapter renders that into the managed env file as a `sh -c` sink appending
the URL to `~/.copilot/pending-oauth-urls.txt`. Authentication becomes operator-
initiated: open the parked URL when you actually want that server.

Caveat: this is global, so `copilot` login will not auto-open a browser either.
The device-code flow still prints its verification URI and code in the TUI.

### §11.4 `mcpDefaultDisabled` — servers configured but not connected

`disabledMcpServers` (settings) keeps a server fully configured while leaving it
unconnected, which makes `/mcp enable <name>` an on-demand connect switch. GitHub's
docs describe `/mcp disable` as applying "for the current session"; it does not —
the command writes `disabledMcpServers` to `~/.copilot/settings.json` and a fresh
process honours it:

```
$ copilot mcp list          # settings.json: {}
  probe-a (local)           probe-b (local)
$ copilot mcp list          # settings.json: {"disabledMcpServers":["probe-a"]}
  probe-a (local, disabled) probe-b (local)
```

The `_agentihooks.mcpDefaultDisabled` directive applies that to every configured
server after MCP registration, including servers agentihooks does not manage (they
are in the same `mcp-config.json`). `mcpAlwaysEnabled` overrides the exempt set,
which defaults to `hooks-utils` — disabling the toolbelt would remove the fleet
tools from the session.

Copilot records a hand-enable in `enabledMcpServers`, and the installer never
re-disables a name found there, so `/mcp enable` survives the next install.

### §11.5 `browserCommand` — which browser gets the OAuth URL

Copilot picks the OAuth browser per platform: `open` on macOS, `xdg-open` on
Linux, `cmd /c start` on Windows. Under WSL the Linux branch applies, so
`xdg-open` reaches whatever browser is installed *inside the distro* — which
carries none of the operator's Windows sessions. A Microsoft authorization opened
there can never complete: the browser has no session to authorize with.

`COPILOT_DEBUG_BROWSER` is consulted ahead of every launch path (before the
remote-environment skip, before `$BROWSER`, before the per-platform default). It
holds a JSON string array; Copilot spawns `array[0]` with the remaining elements
plus the URL appended. `_agentihooks.browserCommand` renders into it and accepts
either form:

```json
"browserCommand": "auto"
"browserCommand": "\"/mnt/c/Program Files/Google/Chrome/Application/chrome.exe\" --new-tab"
```

A string is shell-split; an array is passed through. Every launcher receives the
URL as argv, which matters: an authorization URL is full of `&`, so anything
routed through `cmd /c start` would be truncated at the first one.

**`explorer.exe` is the obvious choice and it is wrong.** Copilot spawns the
launcher with its own working directory, which under WSL is a Linux path;
explorer.exe cannot resolve one, ignores the URL, and opens a File Explorer
window on Documents instead. Observed live. It exits 1 while doing so, and
Copilot reports only a *spawn* failure, so nothing surfaces the misfire.

`auto` therefore probes, in order: `wslview`, Windows Chrome (both Program Files
locations), Windows Edge. Verified from a Linux cwd with a `&`-bearing URL:
`wslview` exits 0 and opens the URL; `explorer.exe` exits 1 and opens Documents.
If none resolve, nothing is written and the install warns — `wslu` supplies
`wslview` and is the smallest fix.

A bundle profile is installed on more than one machine, so the value resolves at
install time rather than being written through blindly:

| value | WSL | macOS / native Linux |
|---|---|---|
| `"auto"` | first of `wslview`, Chrome, Edge that resolves | nothing written — Copilot's `open` / `xdg-open` |
| explicit command | written if it resolves | dropped with a warning if it does not |
| absent | nothing written | nothing written |

`"auto"` is the portable form: the OAuth URL reaches the Windows default browser
under WSL and the operator's own default browser everywhere else. An explicit
command that does not exist on the machine is dropped rather than written —
Copilot reports only a *spawn* failure to its debug log, so an unresolvable
launcher would silently open nothing, which is worse than the default it replaced.

`suppressBrowserLaunch` (§11.3) is the same mechanism pointed at a sink;
`browserCommand` wins if both are set. A value that is neither a string nor a list
of strings is dropped with a warning — the install must degrade to Copilot's own
browser, not abort on a typo.

One undocumented Copilot detail, confirmed in `app.js`: the web-flow login path
substitutes the URL for a literal `"%s"` element if the array contains one,
instead of appending. Nothing agentihooks emits contains `%s`, so the append
behaviour above is what fires.

### §11.6 `channels` — broadcast subscriptions have no settings home in Copilot

Claude carries `AGENTIHOOKS_BASE_CHANNELS` in its settings `env` block. Copilot's
settings catalogue has no `env` key (only `envValueMode`, which is unrelated), so
that mechanism does not port. Left alone, a Copilot session reads an unset
variable, subscribes to nothing, and every channel-targeted broadcast passes it
by — visible as an empty `channels:` on the statusline:

```
agentihooks: smith,brain  settings:smith,brain  channels:
```

`_agentihooks.channels` renders the list into the managed env file, which the
installer's `agentienv` shell block sources with `set -a`. Copilot inherits the
exported variable, and so do the hooks, the statusline command and the MCP server
it spawns. A list or a comma string are both accepted.

**The reach stops at the shell.** `agentienv` runs from `~/.bashrc`, which bash
sources only for interactive shells, so only a Copilot descending from one gets
the variable. Measured:

```
bash -c  '...'  → AGENTIHOOKS_BASE_CHANNELS UNSET   (non-interactive)
bash -lc '...'  → AGENTIHOOKS_BASE_CHANNELS UNSET   (login, non-interactive)
bash -ic '...'  → agentienv loads; variable SET
```

A Copilot launched by an IDE extension, a systemd unit, or any non-interactive
spawn subscribes to nothing. That is the same limit every variable in that file
carries, `COPILOT_ALLOW_ALL` included; a target-native settings key would not have
it, and Copilot exposes none.

The file is install-time codegen: it materialises on the next
`agentihooks init --target copilot`, not retroactively on an install that predates
the directive.

`profiles/_base/settings.base.copilot.json` ships `brain,amygdala`, matching the
Claude default in `profiles/default/.claude/settings.overrides.json`; a test pins
the two together so a copilot session lands on the same channels a claude session
does.

### §11.7 `COPILOT_ALLOW_ALL` must be the literal `true`

The variable is read two different ways, and only one of them is truthy-tolerant.

`--allow-all-tools` is declared with Commander's `.env("COPILOT_ALLOW_ALL")`, which
binds on the variable being **present and non-empty**. So `COPILOT_ALLOW_ALL=1`
does grant the tools axis, and a probe that only exercises a tool call reports
success.

Every other consumer compares `process.env.COPILOT_ALLOW_ALL === "true"` — an
exact string. Those gate folder trust, workspace MCP source discovery, repo hook
loading in prompt mode, and plugin activation. With `1` they all stay off,
silently, while the tools axis works — the failure mode is a switch that looks
set because the visible half of it is.

`copilot help environment` documents it as: *allow all tools to run automatically
without confirmation when set to "true"*. Take that literally.

Note also that allow-all is not one flag but three axes — `getAllowAllPermissionStatus()`
returns `baseline: {tools, paths, urls}`, and `--allow-all-paths` is a separate
flag. Granting tools does not grant paths.
