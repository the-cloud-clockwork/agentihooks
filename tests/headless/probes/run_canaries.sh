#!/usr/bin/env bash
set -euo pipefail

# Manual probe, never run in CI: spends real Claude and Codex quota.
# Usage: run_canaries.sh <out dir> [run name ...]

OUT="$(readlink -f "$1")"
shift
RUNS=("$@")
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PY="${HDL0_PYTHON:-$HOME/dev/tcc-ecosystem/.venv/bin/python}"
CLAUDE_ACCOUNT="${HDL0_CLAUDE_ACCOUNT:-$(compgen -e | grep '^AH_CC_TOKEN_' | sort | head -1 || true)}"
[[ -n "$CLAUDE_ACCOUNT" ]] || { echo "run_canaries: no AH_CC_TOKEN_* in the environment" >&2; exit 2; }

WORK="$OUT/work"
CLAUDE_HOME="$OUT/claude-home"
CODEX_HOME_DIR="$OUT/codex-home"
SCHEMA="$OUT/schema.json"
mkdir -p "$WORK" "$CLAUDE_HOME" "$CODEX_HOME_DIR" "$OUT/codex-empty-home" "$OUT/runs"

if [[ ! -f "$WORK/canary.txt" ]]; then
  printf 'hdl0-%s\nsecond line\n' "$(head -c 16 /dev/urandom | sha256sum | cut -c1-12)" >"$WORK/canary.txt"
fi

cat >"$SCHEMA" <<'JSON'
{"type":"object","properties":{"answer":{"type":"string"},"tools_used":{"type":"array","items":{"type":"string"}}},"required":["answer","tools_used"],"additionalProperties":false}
JSON

"$PY" - "$CLAUDE_HOME" "$CODEX_HOME_DIR" "$WORK" <<'PY'
import json, sys
from pathlib import Path

claude_home, codex_home, work = map(Path, sys.argv[1:])
marker = work.parent / "mcp.log"
mcp = {"command": "sh", "args": ["-c", f"echo started >> {marker}; exit 1"]}
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"]

def entry(event):
    return [{"hooks": [{"type": "command", "command": f'cat >/dev/null; echo {event} >> "$HDL0_HOOK_LOG"'}]}]

(claude_home / "settings.json").write_text(json.dumps(
    {"hooks": {e: entry(e) for e in events}, "enableAllProjectMcpServers": True}, indent=1))
(claude_home / "CLAUDE.md").write_text("Begin every answer with the word USERMD.\n")
(codex_home / "hooks.json").write_text(json.dumps({"hooks": {e: entry(e) for e in events}}, indent=1))
(codex_home / "config.toml").write_text(
    f'[features]\nhooks = true\n\n[mcp_servers.canary]\ncommand = "sh"\nargs = {json.dumps(mcp["args"])}\n')
(codex_home / "AGENTS.md").write_text("Begin every answer with the word USERMD.\n")
for name in ("CLAUDE.md", "AGENTS.md"):
    (work / name).write_text("Begin every answer with the word PROJECTMD.\n")
(work / ".mcp.json").write_text(json.dumps({"mcpServers": {"canary": mcp}}))
PY

READ_PROMPT='Read the file canary.txt in the current directory and put its exact first line in answer. If you have no way to read files, put UNREADABLE in answer. List every tool you called in tools_used.'
WRITE_PROMPT='Create a file named written.txt in the current directory containing the word WRITTEN. Put DONE in answer if the file now exists, otherwise FAILED. List every tool you called in tools_used.'
TOOLS_PROMPT='Put the exact names of every tool or function you can call, comma separated, in answer, or NONE. Put nothing in tools_used.'
SYSTEM_PROMPT='You are a probe. Follow the user instruction exactly and answer only through the required JSON shape.'

CLAUDE_SEALED=(-p --tools "" --strict-mcp-config --setting-sources "" --settings '{"disableAllHooks":true}'
  --no-session-persistence --disable-slash-commands --system-prompt "$SYSTEM_PROMPT"
  --json-schema "$(cat "$SCHEMA")" --output-format stream-json --verbose --model sonnet --effort low)
CLAUDE_READONLY=(-p --tools "Read,Grep,Glob,WebSearch,WebFetch" --permission-mode dontAsk --strict-mcp-config
  --setting-sources "" --settings '{"disableAllHooks":true}' --no-session-persistence --disable-slash-commands
  --system-prompt "$SYSTEM_PROMPT" --json-schema "$(cat "$SCHEMA")" --output-format stream-json --verbose
  --model sonnet --effort low)
CODEX_COMMON=(exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only
  --output-schema "$SCHEMA" --json -c model_reasoning_effort=\"low\")
CODEX_SEALED=("${CODEX_COMMON[@]}" --disable shell_tool --disable unified_exec --disable code_mode
  --disable code_mode_host --disable apps --disable plugins --disable multi_agent --disable multi_agent_v2
  --disable goals --disable sleep_tool --disable view_image --disable image_generation --disable browser_use
  --disable computer_use --disable skill_search --disable tool_suggest --disable hooks
  -c web_search=\"disabled\" -c project_doc_max_bytes=0 -c model_catalog_json=\"$OUT/catalog-sealed.json\")
CODEX_READONLY=("${CODEX_COMMON[@]}" --disable apps --disable plugins --disable multi_agent --disable hooks)

run_claude() {
  local name="$1" prompt="$2"
  shift 2
  local dir="$OUT/runs/$name"
  rm -rf "$dir" "$WORK/written.txt" "$OUT/mcp.log"
  mkdir -p "$dir"
  printf '%q ' claude "$@" "$prompt" >"$dir/command.txt"
  (cd "$WORK" && env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$CLAUDE_HOME" \
    CLAUDE_CODE_OAUTH_TOKEN="${!CLAUDE_ACCOUNT}" HDL0_HOOK_LOG="$dir/hooks.log" \
    timeout 300 "${HDL0_CLAUDE_BIN:-claude}" "$@" "$prompt" >"$dir/stdout.jsonl" 2>"$dir/stderr.txt") && echo 0 >"$dir/exit" || echo $? >"$dir/exit"
  [[ -f "$WORK/written.txt" ]] && echo present >"$dir/written" || echo absent >"$dir/written"
  [[ -f "$OUT/mcp.log" ]] && mv "$OUT/mcp.log" "$dir/mcp.log" || true
}

run_codex() {
  local name="$1" prompt="$2"
  shift 2
  [[ -n "${HDL0_CODEX_MODEL:-}" ]] && set -- "$@" -m "$HDL0_CODEX_MODEL"
  local dir="$OUT/runs/$name"
  rm -rf "$dir" "$WORK/written.txt" "$OUT/mcp.log"
  mkdir -p "$dir"
  printf '%q ' codex "$@" -o "$dir/last.json" "$prompt" >"$dir/command.txt"
  (cd "$WORK" && CODEX_HOME="${HDL0_RUN_HOME:-$CODEX_HOME_DIR}" HDL0_HOOK_LOG="$dir/hooks.log" PYTHONPATH="$REPO" \
    timeout 300 "$PY" "$REPO/tests/headless/probes/codex_launch.py" "$@" -o "$dir/last.json" "$prompt" \
    >"$dir/stdout.jsonl" 2>"$dir/stderr.txt" </dev/null) && echo 0 >"$dir/exit" || echo $? >"$dir/exit"
  [[ -f "$WORK/written.txt" ]] && echo present >"$dir/written" || echo absent >"$dir/written"
  [[ -f "$OUT/mcp.log" ]] && mv "$OUT/mcp.log" "$dir/mcp.log" || true
}

want() {
  [[ ${#RUNS[@]} -eq 0 ]] && return 0
  local r
  for r in "${RUNS[@]}"; do [[ "$r" == "$1" ]] && return 0; done
  return 1
}

if [[ ! -f "$OUT/catalog-sealed.json" ]]; then
  CODEX_HOME="$OUT/codex-empty-home" PYTHONPATH="$REPO" "$PY" "$REPO/tests/headless/probes/codex_launch.py" debug models \
    </dev/null 2>"$OUT/catalog.err" | "$PY" "$REPO/tests/headless/probes/sealed_catalog.py" >"$OUT/catalog-sealed.json"
fi

want claude-control && run_claude claude-control "$READ_PROMPT" -p --no-session-persistence --output-format stream-json --verbose --model sonnet --effort low
want claude-bare && run_claude claude-bare "$READ_PROMPT" --bare -p --tools "" --no-session-persistence --output-format stream-json --verbose --model sonnet
want claude-sealed-sources-only && run_claude claude-sealed-sources-only "$READ_PROMPT" -p --tools "" --setting-sources "" --no-session-persistence --output-format stream-json --verbose --model sonnet --effort low
want claude-sealed-read && run_claude claude-sealed-read "$READ_PROMPT" "${CLAUDE_SEALED[@]}"
want claude-sealed-write && run_claude claude-sealed-write "$WRITE_PROMPT" "${CLAUDE_SEALED[@]}"
want claude-readonly-read && run_claude claude-readonly-read "$READ_PROMPT" "${CLAUDE_READONLY[@]}"
want claude-readonly-write && run_claude claude-readonly-write "$WRITE_PROMPT" "${CLAUDE_READONLY[@]}"

want codex-control && run_codex codex-control "$READ_PROMPT" exec --skip-git-repo-check --json --sandbox read-only --dangerously-bypass-hook-trust
want codex-untrusted && run_codex codex-untrusted "$READ_PROMPT" exec --skip-git-repo-check --json --sandbox read-only
want codex-ignore-config-hooks && run_codex codex-ignore-config-hooks "$READ_PROMPT" "${CODEX_COMMON[@]}" --enable hooks --dangerously-bypass-hook-trust
EMPTY="$OUT/codex-empty-home"
want codex-sealed-read && HDL0_RUN_HOME="$EMPTY" run_codex codex-sealed-read "$READ_PROMPT" "${CODEX_SEALED[@]}"
want codex-sealed-write && HDL0_RUN_HOME="$EMPTY" run_codex codex-sealed-write "$WRITE_PROMPT" "${CODEX_SEALED[@]}"
want codex-sealed-tools && HDL0_RUN_HOME="$EMPTY" run_codex codex-sealed-tools "$TOOLS_PROMPT" "${CODEX_SEALED[@]}"
want codex-sealed-hooks && run_codex codex-sealed-hooks "$READ_PROMPT" "${CODEX_SEALED[@]}" --dangerously-bypass-hook-trust
want codex-readonly-read && HDL0_RUN_HOME="$EMPTY" run_codex codex-readonly-read "$READ_PROMPT" "${CODEX_READONLY[@]}"
want codex-readonly-write && HDL0_RUN_HOME="$EMPTY" run_codex codex-readonly-write "$WRITE_PROMPT" "${CODEX_READONLY[@]}"

"$PY" "$REPO/tests/headless/probes/summarize.py" "$OUT"
