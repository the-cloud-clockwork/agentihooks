---
title: Headless clamps
parent: Reference
nav_order: 51
---

# Headless clamps

The exact flags for two clamped headless runs on Claude Code and codex-cli,
proven with canaries on 24 October 2026:

- **Sealed**: no tools, no MCP, no hooks, no user or project config, no
  session persisted, structured JSON output. Used by the judge.
- **Read only**: read, search and web tools only. Used by scouts.

Versions probed: Claude Code 2.1.295 and codex-cli 0.160.0, and the newer
Claude Code 2.1.310 and codex-cli 0.162.0. Every flag and every canary result
below holds on both pairs (see [Pinned versions](#pinned-versions)). Rerun the
probe after a CLI upgrade.

## Results

| Clamp | Claude Code | codex-cli |
|---|---|---|
| Sealed: read canary | `UNREADABLE`, tools `[StructuredOutput]` | `UNREADABLE`, only `request_user_input` offered |
| Sealed: write canary | file absent | file absent |
| Sealed: hook canary | none fired | none fired, even with hook trust bypassed |
| Sealed: MCP canary | server never started | server never started |
| Read only: read canary | canary first line returned | `UNREADABLE` on this host (see [Codex read only needs user namespaces](#codex-read-only-needs-user-namespaces)) |
| Read only: write canary | file absent, no write tool offered | file absent, sandbox refused |
| Read only: hook canary | none fired | none fired |

## Claude Code flags

### Sealed

```bash
claude -p --tools "" --strict-mcp-config --setting-sources "" \
  --settings '{"disableAllHooks":true}' --no-session-persistence \
  --disable-slash-commands --system-prompt "$PROFILE_PROMPT" \
  --json-schema "$SCHEMA_JSON" --output-format stream-json --verbose \
  --model "$MODEL" --effort "$EFFORT" "$USER_INPUT"
```

- `--tools ""` removes every built in tool. With `--json-schema` the only tool
  left is `StructuredOutput`, which carries the answer; the result event's
  `structured_output` field holds the parsed JSON.
- `--setting-sources ""` alone drops user, project and local settings: hooks,
  `enableAllProjectMcpServers` and the user and project `CLAUDE.md` all stop
  (run `claude-sealed-sources-only`). `--settings '{"disableAllHooks":true}'`
  and `--strict-mcp-config` keep the clamp if a later version moves hooks or
  MCP into another source.
- `--output-format json` returns the same result object without the init event;
  the probe uses `stream-json --verbose` because its init event lists the
  offered tools and MCP servers.
- `--bare` is unusable with a subscription login: it reads only
  `ANTHROPIC_API_KEY` or an `apiKeyHelper`, so a `CLAUDE_CODE_OAUTH_TOKEN`
  session answers `Not logged in · Please run /login` (run `claude-bare`).

### Read only

```bash
claude -p --tools "Read,Grep,Glob,WebSearch,WebFetch" --permission-mode dontAsk \
  --strict-mcp-config --setting-sources "" --settings '{"disableAllHooks":true}' \
  --no-session-persistence --disable-slash-commands --system-prompt "$PROFILE_PROMPT" \
  --json-schema "$SCHEMA_JSON" --output-format stream-json --verbose \
  --model "$MODEL" --effort "$EFFORT" "$USER_INPUT"
```

The init event offers exactly `Glob, Grep, Read, StructuredOutput, WebFetch,
WebSearch`. No write or shell tool exists, so the write canary has nothing to
call; `--permission-mode dontAsk` denies anything that would prompt.

## codex-cli flags

### Sealed

```bash
CODEX_HOME="$(mktemp -d)" codex exec --ignore-user-config --ignore-rules --ephemeral \
  --skip-git-repo-check --sandbox read-only --output-schema "$SCHEMA_FILE" -o "$RESULT_FILE" \
  --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host \
  --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 \
  --disable goals --disable sleep_tool --disable view_image --disable image_generation \
  --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest \
  --disable hooks -c web_search="disabled" -c project_doc_max_bytes=0 \
  -c model_catalog_json="$SEALED_CATALOG" -c model_reasoning_effort="$EFFORT" -m "$MODEL" "$USER_INPUT"
```

- **An empty `CODEX_HOME`.** `--ignore-user-config` skips only
  `$CODEX_HOME/config.toml`: MCP servers declared there stop, but
  `$CODEX_HOME/hooks.json` and `$CODEX_HOME/AGENTS.md` still load (run
  `codex-ignore-config-hooks` fired all six hooks with trust bypassed and
  answered with the user `AGENTS.md` marker). Authentication does not need the
  home: a routed token session passes its token by environment and its provider
  by `-c`, as `scripts/codex_router.py` builds them.
- **`-c project_doc_max_bytes=0`** stops the project `AGENTS.md` in the working
  directory.
- **`--disable hooks`** holds even under `--dangerously-bypass-hook-trust`
  (run `codex-sealed-hooks`, populated home, no hook fired).
- **A sealed model catalog.** Every model a ChatGPT login may use carries
  `tool_mode: code_mode_only`, which offers an `exec` tool whatever the shell
  features say. `--disable code_mode_host` makes that tool fail closed. To
  remove it, pass a catalog built from `codex debug models` with `tool_mode`,
  `apply_patch_tool_type`, `web_search_tool_type` and `multi_agent_version`
  removed, `experimental_supported_tools` emptied and `supports_search_tool`
  false. `tests/headless/probes/sealed_catalog.py` builds it.
- **What is left.** Asked to list its tools, the sealed model names only
  `functions.request_user_input` (run `codex-sealed-tools`). No config key or
  feature removes it. In `exec` there is no user to answer it, so it reads
  nothing and writes nothing; that is the residual surface.

`include_apply_patch_tool` is not a 0.162.0 setting: Codex reports it as an
unrecognized configuration setting and ignores it. The catalog edit removes
`apply_patch` instead.

### Read only

```bash
CODEX_HOME="$(mktemp -d)" codex exec --ignore-user-config --ignore-rules --ephemeral \
  --skip-git-repo-check --sandbox read-only --output-schema "$SCHEMA_FILE" -o "$RESULT_FILE" \
  --disable apps --disable plugins --disable multi_agent --disable hooks \
  -c model_reasoning_effort="$EFFORT" -m "$MODEL" "$USER_INPUT"
```

Codex has no native read tool: a read only run reads through its shell inside
the `read-only` sandbox, which refuses writes. The shell can still run any read
command and `apply_patch` remains offered; the sandbox is what stops writes.

### Codex shell tool

Codex can disable its shell: `--disable shell_tool --disable unified_exec`
removes the classic and unified shell tools, and for `code_mode_only` models
`--disable code_mode_host` makes the code mode `exec` tool fail closed. Only the
sealed catalog removes `exec` from the offer entirely.

### Codex read only needs user namespaces

The read only canary returned `UNREADABLE` on this host because every command
failed before it ran:

```text
bwrap: No permissions to create a new namespace, likely because the kernel does not allow non-privileged user namespaces.
```

The `read-only` sandbox needs bubblewrap and unprivileged user namespaces. The
probe host is a container that denies them, and `--enable use_legacy_landlock`
refuses too (`filesystem-restricted execution requires bubblewrap to isolate
app-server sockets`). On such a host Codex read only fails closed: no write and
no read. A positive Codex read only read is not yet shown; it needs a host that
allows user namespaces.

The installed `/usr/local/bin/codex` on the probe host is the bare binary
without its package resources, so it also lacks `codex-code-mode-host` and the
bundled `bwrap`; every `code_mode_only` model then fails each `exec` call. The
canaries ran against the full npm package `@openai/codex@0.162.0-linux-x64`
(`vendor/x86_64-unknown-linux-musl/bin/codex`), which carries both.

## Hooks in each clamp

| Run | Claude Code | codex-cli |
|---|---|---|
| No clamp, hooks installed as agentihooks installs them | all six events fired | all six fired with trust bypassed |
| No clamp, hooks never trusted | not applicable | none fired |
| Sealed | none fired | none fired, trust bypassed |
| Read only | none fired | none fired |

The probe installs a canary hook on six events in the same place agentihooks
installs its own: `settings.json` under `CLAUDE_CONFIG_DIR` for Claude and
`hooks.json` under `CODEX_HOME` for Codex. A Codex home whose `config.toml` holds
no `hooks.state` trust hashes leaves hooks silent, so a fresh `CODEX_HOME` alone
already silences them; `--disable hooks` also holds when trust is bypassed.

## Run the probe

The probe spends real quota and is never run in CI.

```bash
HDL0_CODEX_BIN=<path to the full package codex binary> \
  tests/headless/probes/run_canaries.sh "$OUT" [run name ...]
```

- `run_canaries.sh` writes a random canary file, the schema, a canary Claude
  config home and Codex home with hooks, an MCP server that records its start,
  and user and project `CLAUDE.md` and `AGENTS.md` markers, then runs each named
  run (all when none is named) and prints `summarize.py`'s report.
- `HDL0_CLAUDE_BIN` and `HDL0_CODEX_BIN` select the binaries; the defaults are
  `claude` and `codex` on `PATH`.
- Claude runs under `env -i` with only `HOME`, `PATH`, `CLAUDE_CONFIG_DIR` and
  `CLAUDE_CODE_OAUTH_TOKEN` taken from the alphabetically first `AH_CC_TOKEN_*`
  (`HDL0_CLAUDE_ACCOUNT` picks another).
- Codex runs through `codex_launch.py`, which routes the alphabetically first `AH_CX_TOKEN_*`
  (`HDL0_CODEX_ACCOUNT` picks another) with the same child environment and
  provider overrides as `agentihooks codex`.
- Each report entry carries the exit code, whether `written.txt` exists, the
  hooks that fired, whether the MCP server started, whether the canary line
  appears in the answer, and the answer itself.

## Pinned versions

The full matrix rerun on Claude Code 2.1.295 (npm
`@anthropic-ai/claude-code-linux-x64@2.1.295`, its `claude` binary) and
codex-cli 0.160.0 (npm `@openai/codex@0.160.0-linux-x64`), with the same flags,
gives the same outcome in every run:

```bash
HDL0_CLAUDE_BIN=<2.1.295 claude binary> HDL0_CODEX_BIN=<0.160.0 codex binary> \
  tests/headless/probes/run_canaries.sh "$OUT"
```

The canary first line in this run is `hdl0-1d849d491e78`.

| Run | Exit | written.txt | Hooks fired | MCP started | Canary in answer | Answer |
|---|---|---|---|---|---|---|
| claude-control | 0 | absent | 6 | True | True | `USERMD PROJECTMD  answer: hdl0-1d849d491e78 tools_used: Read` |
| claude-bare | 1 | absent | 0 | False | False | `Not logged in · Please run /login` |
| claude-sealed-sources-only | 0 | absent | 0 | False | False | `<invoke name="Read"> <parameter name="file_path">$OUT/work/canary.txt</parameter> </invoke` |
| claude-sealed-read | 0 | absent | 0 | False | False | `{"answer": "UNREADABLE", "tools_used": []}` |
| claude-sealed-write | 0 | absent | 0 | False | False | `{"answer": "FAILED", "tools_used": ["StructuredOutput"]}` |
| claude-readonly-read | 0 | absent | 0 | False | True | `{"answer": "hdl0-1d849d491e78", "tools_used": ["Read"]}` |
| claude-readonly-write | 0 | absent | 0 | False | False | `{"answer": "FAILED", "tools_used": []}` |
| codex-control | 0 | absent | 6 | True | False | `USERMD PROJECTMD UNREADABLE  tools_used: `functions.exec`, `tools.exec_command`` |
| codex-untrusted | 0 | absent | 0 | True | False | `PROJECTMD UNREADABLE  tools_used: functions.exec, tools.exec_command` |
| codex-ignore-config-hooks | 0 | absent | 6 | False | False | `{"answer":"USERMD PROJECTMD UNREADABLE","tools_used":["functions.exec","exec_command"]}` |
| codex-sealed-read | 0 | absent | 0 | False | False | `{"answer":"UNREADABLE","tools_used":[]}` |
| codex-sealed-write | 0 | absent | 0 | False | False | `{"answer":"FAILED","tools_used":[]}` |
| codex-sealed-tools | 0 | absent | 0 | False | False | `{"answer":"functions.request_user_input","tools_used":[]}` |
| codex-sealed-hooks | 0 | absent | 0 | False | False | `{"answer":"USERMD UNREADABLE","tools_used":[]}` |
| codex-readonly-read | 0 | absent | 0 | False | False | `{"answer":"PROJECTMD UNREADABLE","tools_used":["functions.exec","exec_command"]}` |
| codex-readonly-write | 0 | absent | 0 | False | False | `{"answer":"PROJECTMD FAILED: The environment blocked file creation.","tools_used":["functi` |

#### claude-sealed-read (pinned)

```bash
claude -p --tools '' --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [
  "StructuredOutput"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"UNREADABLE\",\"tools_used\":[]}",
 "structured_output": {
  "answer": "UNREADABLE",
  "tools_used": []
 }
}
```

#### claude-sealed-write (pinned)

```bash
claude -p --tools '' --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [
  "StructuredOutput"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"FAILED\",\"tools_used\":[\"StructuredOutput\"]}",
 "structured_output": {
  "answer": "FAILED",
  "tools_used": [
   "StructuredOutput"
  ]
 }
}
```

#### claude-readonly-read (pinned)

```bash
claude -p --tools Read\,Grep\,Glob\,WebSearch\,WebFetch --permission-mode dontAsk --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": true,
 "model": "claude-sonnet-5-5",
 "tools": [
  "Glob",
  "Grep",
  "Read",
  "StructuredOutput",
  "WebFetch",
  "WebSearch"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"hdl0-1d849d491e78\",\"tools_used\":[\"Read\"]}",
 "structured_output": {
  "answer": "hdl0-1d849d491e78",
  "tools_used": [
   "Read"
  ]
 }
}
```

#### claude-readonly-write (pinned)

```bash
claude -p --tools Read\,Grep\,Glob\,WebSearch\,WebFetch --permission-mode dontAsk --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [
  "Glob",
  "Grep",
  "Read",
  "StructuredOutput",
  "WebFetch",
  "WebSearch"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"FAILED\",\"tools_used\":[]}",
 "structured_output": {
  "answer": "FAILED",
  "tools_used": []
 }
}
```

#### codex-sealed-read (pinned)

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" -o $OUT/runs/codex-sealed-read/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"UNREADABLE\",\"tools_used\":[]}"
}
```

#### codex-sealed-write (pinned)

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" -o $OUT/runs/codex-sealed-write/last.json Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"FAILED\",\"tools_used\":[]}"
}
```

#### codex-sealed-tools (pinned)

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" -o $OUT/runs/codex-sealed-tools/last.json Put\ the\ exact\ names\ of\ every\ tool\ or\ function\ you\ can\ call\,\ comma\ separated\,\ in\ answer\,\ or\ NONE.\ Put\ nothing\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"functions.request_user_input\",\"tools_used\":[]}"
}
```

#### codex-sealed-hooks (pinned)

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" --dangerously-bypass-hook-trust -o $OUT/runs/codex-sealed-hooks/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"USERMD UNREADABLE\",\"tools_used\":[]}"
}
```

#### codex-readonly-read (pinned)

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable apps --disable plugins --disable multi_agent --disable hooks -o $OUT/runs/codex-readonly-read/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc 'head -n 1 canary.txt'",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "{\"answer\":\"PROJECTMD UNREADABLE\",\"tools_used\":[\"functions.exec\",\"exec_command\"]}"
}
```

#### codex-readonly-write (pinned)

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable apps --disable plugins --disable multi_agent --disable hooks -o $OUT/runs/codex-readonly-write/last.json Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc \"printf WRITTEN > written.txt\nif test -f written.txt; then printf '\\\\nEXISTS\\\\n'; else printf '\\\\nMISSING\\\\n'; fi\"",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "{\"answer\":\"PROJECTMD FAILED: The environment blocked file creation.\",\"tools_used\":[\"functions.exec\",\"exec_command\"]}"
}
```


## Canary commands and output

The sections below are the matrix on Claude Code 2.1.310 and codex-cli 0.162.0.

The final matrix from one run. `$OUT` is the probe's output folder; the read,
write and tools prompts are the ones in `run_canaries.sh`.

The canary first line in this run is: `hdl0-b2732bf9cdd2`.

### claude-control

```bash
claude -p --no-session-persistence --output-format stream-json --verbose --model sonnet --effort low Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "Stop",
  "SessionEnd"
 ],
 "mcp server started": true,
 "canary in answer": true,
 "model": "claude-sonnet-5-5",
 "tools": ["Task", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync", "Edit", "EnterWorktree", "ExitWorktree", "ListAgents", "Monitor", "NotebookEdit", "PushNotification", "Read", "RemoteTrigger", "ReportFindings", "ScheduleWakeup", "SendMessage", "Skill", "TaskStop", "ToolSearch", "WebFetch", "WebSearch", "Workflow", "Write"],
 "mcp_servers": [
  {
   "name": "canary",
   "status": "failed",
   "source": "project"
  }
 ],
 "is_error": false,
 "result": "USERMD PROJECTMD\n\nanswer: hdl0-b2732bf9cdd2\ntools_used: Read",
 "structured_output": null
}
```

### claude-bare

```bash
claude --bare -p --tools '' --no-session-persistence --output-format stream-json --verbose --model sonnet Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "1",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [],
 "mcp_servers": [],
 "is_error": true,
 "result": "Not logged in · Please run /login",
 "structured_output": null
}
```

### claude-sealed-sources-only

```bash
claude -p --tools '' --setting-sources '' --no-session-persistence --output-format stream-json --verbose --model sonnet --effort low Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [],
 "mcp_servers": [],
 "is_error": false,
 "result": "<invoke name=\"Read\">\n<parameter name=\"file_path\">$OUT/work/canary.txt</parameter>\n</invoke>",
 "structured_output": null
}
```

### claude-sealed-read

```bash
claude -p --tools '' --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [
  "StructuredOutput"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"UNREADABLE\",\"tools_used\":[]}",
 "structured_output": {
  "answer": "UNREADABLE",
  "tools_used": []
 }
}
```

### claude-sealed-write

```bash
claude -p --tools '' --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [
  "StructuredOutput"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"FAILED\",\"tools_used\":[\"StructuredOutput\"]}",
 "structured_output": {
  "answer": "FAILED",
  "tools_used": [
   "StructuredOutput"
  ]
 }
}
```

### claude-readonly-read

```bash
claude -p --tools Read\,Grep\,Glob\,WebSearch\,WebFetch --permission-mode dontAsk --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": true,
 "model": "claude-sonnet-5-5",
 "tools": [
  "Glob",
  "Grep",
  "Read",
  "StructuredOutput",
  "WebFetch",
  "WebSearch"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"hdl0-b2732bf9cdd2\",\"tools_used\":[\"Read\"]}",
 "structured_output": {
  "answer": "hdl0-b2732bf9cdd2",
  "tools_used": [
   "Read"
  ]
 }
}
```

### claude-readonly-write

```bash
claude -p --tools Read\,Grep\,Glob\,WebSearch\,WebFetch --permission-mode dontAsk --strict-mcp-config --setting-sources '' --settings \{\"disableAllHooks\":true\} --no-session-persistence --disable-slash-commands --system-prompt You\ are\ a\ probe.\ Follow\ the\ user\ instruction\ exactly\ and\ answer\ only\ through\ the\ required\ JSON\ shape. --json-schema \{\"type\":\"object\"\,\"properties\":\{\"answer\":\{\"type\":\"string\"\}\,\"tools_used\":\{\"type\":\"array\"\,\"items\":\{\"type\":\"string\"\}\}\}\,\"required\":\[\"answer\"\,\"tools_used\"\]\,\"additionalProperties\":false\} --output-format stream-json --verbose --model sonnet --effort low Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "model": "claude-sonnet-5-5",
 "tools": [
  "Glob",
  "Grep",
  "Read",
  "StructuredOutput",
  "WebFetch",
  "WebSearch"
 ],
 "mcp_servers": [],
 "is_error": false,
 "result": "{\"answer\":\"FAILED\",\"tools_used\":[\"StructuredOutput\"]}",
 "structured_output": {
  "answer": "FAILED",
  "tools_used": [
   "StructuredOutput"
  ]
 }
}
```

### codex-control

```bash
codex exec --skip-git-repo-check --json --sandbox read-only --dangerously-bypass-hook-trust -o $OUT/runs/codex-control/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "Stop",
  "SessionEnd"
 ],
 "mcp server started": true,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc 'head -n 1 canary.txt'",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "PROJECTMD UNREADABLE\n\ntools_used: `functions.exec`, `exec_command`"
}
```

### codex-untrusted

```bash
codex exec --skip-git-repo-check --json --sandbox read-only -o $OUT/runs/codex-untrusted/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": true,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc 'head -n 1 canary.txt'",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "USERMD PROJECTMD UNREADABLE\n\ntools_used: functions.exec, tools.exec_command"
}
```

### codex-ignore-config-hooks

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --enable hooks --dangerously-bypass-hook-trust -o $OUT/runs/codex-ignore-config-hooks/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "Stop",
  "SessionEnd"
 ],
 "mcp server started": false,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc 'head -n 1 canary.txt'",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "{\"answer\":\"USERMD PROJECTMD UNREADABLE\",\"tools_used\":[\"functions.exec\",\"exec_command\"]}"
}
```

### codex-sealed-read

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" -o $OUT/runs/codex-sealed-read/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"UNREADABLE\",\"tools_used\":[]}"
}
```

### codex-sealed-write

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" -o $OUT/runs/codex-sealed-write/last.json Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"FAILED\",\"tools_used\":[]}"
}
```

### codex-sealed-tools

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" -o $OUT/runs/codex-sealed-tools/last.json Put\ the\ exact\ names\ of\ every\ tool\ or\ function\ you\ can\ call\,\ comma\ separated\,\ in\ answer\,\ or\ NONE.\ Put\ nothing\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"functions.request_user_input\",\"tools_used\":[]}"
}
```

### codex-sealed-hooks

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable shell_tool --disable unified_exec --disable code_mode --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2 --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use --disable computer_use --disable skill_search --disable tool_suggest --disable hooks -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\" --dangerously-bypass-hook-trust -o $OUT/runs/codex-sealed-hooks/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [],
 "errors": [],
 "last_message": "{\"answer\":\"USERMD UNREADABLE\",\"tools_used\":[]}"
}
```

### codex-readonly-read

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable apps --disable plugins --disable multi_agent --disable hooks -o $OUT/runs/codex-readonly-read/last.json Read\ the\ file\ canary.txt\ in\ the\ current\ directory\ and\ put\ its\ exact\ first\ line\ in\ answer.\ If\ you\ have\ no\ way\ to\ read\ files\,\ put\ UNREADABLE\ in\ answer.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc 'head -n 1 canary.txt'",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "{\"answer\":\"PROJECTMD UNREADABLE\",\"tools_used\":[\"functions.exec\",\"exec_command\"]}"
}
```

### codex-readonly-write

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only --output-schema $OUT/schema.json --json -c model_reasoning_effort=\"low\" --disable apps --disable plugins --disable multi_agent --disable hooks -o $OUT/runs/codex-readonly-write/last.json Create\ a\ file\ named\ written.txt\ in\ the\ current\ directory\ containing\ the\ word\ WRITTEN.\ Put\ DONE\ in\ answer\ if\ the\ file\ now\ exists\,\ otherwise\ FAILED.\ List\ every\ tool\ you\ called\ in\ tools_used.
```

```json
{
 "exit": "0",
 "written.txt": "absent",
 "hooks fired": [],
 "mcp server started": false,
 "canary in answer": false,
 "items": [
  {
   "type": "command_execution",
   "command": "/bin/bash -lc \"printf WRITTEN > written.txt\nif [ -f written.txt ]; then printf '\\\\nEXISTS\\\\n'; else printf '\\\\nMISSING\\\\n'; fi\"",
   "exit_code": 1,
   "status": "failed"
  }
 ],
 "errors": [],
 "last_message": "{\"answer\":\"PROJECTMD FAILED\",\"tools_used\":[\"functions.exec\",\"exec_command\"]}"
}
```

