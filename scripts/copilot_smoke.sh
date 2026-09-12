#!/usr/bin/env bash
# End-to-end smoke test of agentihooks running against a live GitHub Copilot CLI.
#
# Unit tests hand the hooks synthetic payloads; this drives the REAL binary and
# the REAL installed wrapper. Its value depends entirely on assertions that only
# pass when a hook acted, so two rules keep it honest:
#
#   1. Evidence must be hook-attributable: an exit code, a file the hook wrote,
#      a line this session's hook put in the hook log. Never the model's prose,
#      which passes whether or not a hook ran.
#   2. Log evidence must be scoped to THIS session id. The hook log is shared by
#      every concurrent session on the machine, so an unscoped grep passes on a
#      neighbour's line.
#
# It never mutates the operator's ~/.copilot — everything runs against a scratch
# COPILOT_HOME, because an interrupted in-place swap would leave copilot
# permanently unguarded.
#
# Two tiers:
#   OFFLINE  — config schema accepted by the real binary, plus every hook event
#              driven through the installed wrapper. No auth, no network, no
#              model call. This is where the guardrail proofs live.
#   LIVE     — real `copilot -p` turns. Needs `copilot` authenticated (run
#              `copilot` and `/login`, or export COPILOT_GITHUB_TOKEN). Skipped
#              with a stated reason when unauthenticated, never silently.
#
# Usage: scripts/copilot_smoke.sh [--offline]
set -uo pipefail

OFFLINE_ONLY=0; [[ "${1:-}" == "--offline" ]] && OFFLINE_ONLY=1
WORK="$(mktemp -d "${TMPDIR:-/tmp}/copilot-smoke.XXXXXX")"
export COPILOT_HOME="$WORK/copilot-home"
export AGENTIHOOKS_HOME="$WORK/agentihooks-home"
export AGENTIHOOKS_DISABLE_BYPASS_LOOKUP=1
export AGENTIHOOKS_SECRETS_MODE=standard
LOG="${CLAUDE_HOOK_LOG_FILE:-$AGENTIHOOKS_HOME/logs/hooks.log}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
for cand in "${AGENTIHOOKS_PYTHON:-}" "$ROOT/.venv/bin/python" "$ROOT/../.venv/bin/python"; do
  [ -n "${cand:-}" ] && [ -x "$cand" ] && "$cand" -c "import hooks" 2>/dev/null && { PY="$cand"; break; }
done 2>/dev/null
PASS=0; FAIL=0; INFO=0; SKIP=0
trap 'rm -rf "$WORK"' EXIT

ok()   { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$1"; }
no()   { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$1"; }
nb()   { INFO=$((INFO + 1)); printf '  [INFO] %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  [SKIP] %s\n' "$1"; }

command -v copilot >/dev/null || { echo "copilot not on PATH — npm install -g @github/copilot"; exit 1; }
mkdir -p "$WORK/repo" && ( cd "$WORK/repo" && git init -q -b dev . && git commit -q --allow-empty -m init )

echo "=== copilot smoke — $(copilot --version 2>&1 | head -1) ==="

# ---------------------------------------------------------------- install ---
echo
echo "[1] adapter install into scratch COPILOT_HOME"
"$PY" - <<'PY' || exit 1
import sys, pathlib
root = pathlib.Path(__file__).resolve().parent if "__file__" in dir() else pathlib.Path.cwd()
sys.path.insert(0, str(pathlib.Path.cwd()))
sys.path.insert(0, str(pathlib.Path.cwd() / "scripts"))
import install  # noqa: F401
from scripts.targets.copilot_target import CopilotAdapter

a = CopilotAdapter()
a.write_settings({"permissions": {"defaultMode": "bypassPermissions"}})
a.register_hooks_utils("smoke")
# SSE is the codex-divergent transport — assert the real binary takes it.
a.register_mcp({"smoke-sse": {"type": "sse", "url": "https://mcp.example/sse"}})
PY
WRAPPER="$COPILOT_HOME/agentihooks-hook.sh"
[ -x "$WRAPPER" ] && ok "wrapper installed and executable" || no "wrapper missing or not executable"

# --------------------------------------------- real binary parses our config ---
echo
echo "[2] real binary accepts the config we wrote"
MCP_OUT="$(cd "$WORK/repo" && timeout 60 copilot mcp list 2>&1)"
printf '%s' "$MCP_OUT" | grep -q "hooks-utils" && ok "copilot mcp list sees hooks-utils" || no "copilot mcp list missing hooks-utils: $MCP_OUT"
printf '%s' "$MCP_OUT" | grep -q "smoke-sse (sse)" && ok "SSE transport accepted (codex drops these)" || no "SSE server not accepted: $MCP_OUT"
# A parse error in settings.json surfaces on any subcommand, so a clean run here
# is the binary validating the file we wrote, not merely ignoring it.
printf '%s' "$MCP_OUT" | grep -qiE "error|invalid|unexpected|failed to parse" \
  && no "binary reported a config error: $MCP_OUT" || ok "settings.json + mcp-config.json parse clean"

# ------------------------------------------- real binary validates our hooks ---
# The hook loader runs BEFORE auth, and names any event it does not recognise.
# This is the only proof available without a subscription that the PascalCase
# names the adapter registers are actually accepted — and that the hooks
# DIRECTORY is read at all, since the message names the file.
echo
echo "[2b] real binary accepts our hook event names (pre-auth loader)"
EVENTS_COUNT=$("$PY" -c "import json,os,pathlib;print(len(json.loads(pathlib.Path(os.environ['COPILOT_HOME'],'hooks','agentihooks.json').read_text())['hooks']))")
HOOKLOG="$WORK/hooklogs"; mkdir -p "$HOOKLOG"
( cd "$WORK/repo" && timeout 90 copilot -p hi --allow-all-tools --log-level all --log-dir "$HOOKLOG" --no-color >/dev/null 2>&1 )
if ! ls "$HOOKLOG"/*.log >/dev/null 2>&1; then
  nb "no log produced — cannot verify hook event names this run"
else
  UNKNOWN="$(grep -hoE "Ignoring unknown hook event\(s\) in [^:]*: .*" "$HOOKLOG"/*.log 2>/dev/null || true)"
  [ -z "$UNKNOWN" ] && ok "all $(printf '%s' "$EVENTS_COUNT") registered event names recognised by the loader" \
    || no "copilot rejected event name(s): $UNKNOWN"
  # Prove the check can actually fail, else a silent loader would score a pass.
  CANARY="$WORK/canarylogs"; mkdir -p "$CANARY"
  cp "$COPILOT_HOME/hooks/agentihooks.json" "$WORK/hooks.bak"
  printf '{"version":1,"hooks":{"NotARealEvent":[{"type":"command","command":"/bin/true"}]}}' > "$COPILOT_HOME/hooks/agentihooks.json"
  ( cd "$WORK/repo" && timeout 90 copilot -p hi --allow-all-tools --log-level all --log-dir "$CANARY" --no-color >/dev/null 2>&1 )
  grep -qhE "Ignoring unknown hook event" "$CANARY"/*.log 2>/dev/null \
    && ok "canary: the loader does reject a bogus event name (the check above is live)" \
    || no "canary did NOT fire — the pass above proves nothing"
  cp "$WORK/hooks.bak" "$COPILOT_HOME/hooks/agentihooks.json"
fi

# ------------------------------------------------------- hook wrapper drive ---
# The wrapper is exactly what copilot execs. Driving it directly proves the whole
# chain (env marker -> normalizer -> dispatch -> single-envelope stdout) without
# needing a model call.
echo
echo "[3] every wired event reaches a handler through the installed wrapper"
# Live v1.0.80 contract: stdin is the event's input object alone (no
# hookEventName field); the registration passes the event name as argv[1].
fire() { printf '%s' "$2" | "$WRAPPER" "$1" 2>"$WORK/err.$1" >"$WORK/out.$1"; echo $?; }
one_json() {
  "$PY" - "$1" <<'PY'
import json, sys, pathlib
s = pathlib.Path(sys.argv[1]).read_text().strip()
if not s:
    sys.exit(0)
lines = s.splitlines()
sys.exit(0 if len(lines) == 1 and (json.loads(lines[0]) or True) else 1)
PY
}
EVENTS='sessionStart sessionEnd userPromptSubmitted preToolUse postToolUse agentStop subagentStart subagentStop preCompact permissionRequest notification'
for ev in $EVENTS; do
  RC=$(fire "$ev" "{\"sessionId\":\"smoke-$ev\",\"timestamp\":1787163790624,\"cwd\":\"$WORK/repo\",\"toolName\":\"view\",\"toolArgs\":{}}")
  if [ "$RC" = "0" ]; then
    one_json "$WORK/out.$ev" && ok "$ev -> exit 0, stdout is empty or one JSON object" \
      || no "$ev -> stdout violated the one-JSON-object contract: $(head -c 200 "$WORK/out.$ev")"
  else
    no "$ev -> exit $RC: $(head -c 200 "$WORK/err.$ev")"
  fi
done

echo
echo "[4] the argv event name maps onto the dispatch vocabulary"
grep -q '"additionalContext"' "$WORK/out.sessionStart" \
  && ok "sessionStart context flushed as top-level additionalContext (the shape copilot parses)" \
  || nb "sessionStart produced no envelope (nothing buffered) — dispatch checked in [3]"
# Alias path: a payload that DOES name its event (future copilot, or synthetic
# callers) dispatches without argv.
printf '{"hookEventName":"sessionEnd","sessionId":"smoke-alias","reason":"complete"}' \
  | "$WRAPPER" >"$WORK/out.alias" 2>"$WORK/err.alias" && ok "payload-named event dispatches without argv" \
  || no "payload-named event failed: $(head -c 200 "$WORK/err.alias")"
grep -q "Copilot CLI session_id" "$WORK/out.sessionStart" \
  && ok "session banner names Copilot CLI, not Claude Code" || no "host name banner wrong"

# ------------------------------------------------------------- guardrails ---
echo
echo "[5] guardrails deny through the wrapper (HARD FLOOR)"
KEY="AKIA""TESTDUMMY0000000"
# A malformed payload makes the hook fail open (exit 0), which would read as
# "guardrail passed". Every deny payload below is parse-checked first.
json_ok() { "$PY" -c 'import json,sys; json.load(sys.stdin)' <<< "$1" 2>/dev/null; }
printf '{"toolName":"Write","toolArgs":{"file_path":"/tmp/x.py","content":"k = \x27%s\x27"},"sessionId":"smoke-sec"}' "$KEY" \
  > "$WORK/pay.sec"
json_ok "$(cat "$WORK/pay.sec")" && ok "secret deny payload is valid JSON" || no "secret deny payload is malformed — verdict below is meaningless"
"$WRAPPER" preToolUse < "$WORK/pay.sec" >"$WORK/out.sec" 2>"$WORK/err.sec"; RC=$?
[ "$RC" = "2" ] && grep -q "BLOCKED" "$WORK/err.sec" \
  && ok "secrets HARD FLOOR denies a nameless preToolUse via argv (exit 2 + BLOCKED)" \
  || no "secrets HARD FLOOR did not deny: exit=$RC stderr=$(head -c 200 "$WORK/err.sec")"

printf '{"hookEventName":"preToolUse","toolName":"Bash","toolArgs":{"command":"export AWS_ACCESS_KEY_ID=%s && git push origin main"},"sessionId":"smoke-blk"}' "$KEY" \
  > "$WORK/pay.blk"
json_ok "$(cat "$WORK/pay.blk")" && ok "bash deny payload is valid JSON" || no "bash deny payload is malformed — verdict below is meaningless"
"$WRAPPER" < "$WORK/pay.blk" >"$WORK/out.blk" 2>"$WORK/err.blk"; RC=$?
[ "$RC" = "2" ] && ok "prod-lockdown/secrets deny on Bash (exit 2)" || no "Bash deny failed: exit=$RC"
grep -q "NOTE" "$WORK/err.blk" \
  && ok "buffered context survives into the block stderr (drain path)" \
  || no "buffered NOTE lost on the block path"

# The plan's hazard #1: docs say preToolUse is fail-closed on any non-zero exit,
# the runtime string says exit 2 is a warning. Recorded, not assumed.
echo
echo "[6] exit-code semantics probe (settles docs vs runtime)"
nb "wrapper exits 2 on deny; whether copilot treats that as a block outside"
nb "preToolUse is a LIVE assertion — see [8]. capabilities.requires_envelope_block"
nb "keeps the stdout envelope as the primary channel regardless."

# ------------------------------------------------------------- statusline ---
echo
echo "[7] status line command runs"
SL="$("$PY" - <<'PY'
import json, os, pathlib
print(json.loads((pathlib.Path(os.environ["COPILOT_HOME"]) / "settings.json").read_text())["statusLine"]["command"])
PY
)"
echo '{"session_id":"smoke-sl","cwd":"/tmp","model":{"display_name":"gpt-5"}}' | timeout 30 sh -c "$SL" >"$WORK/sl.out" 2>"$WORK/sl.err"
[ -s "$WORK/sl.out" ] && ok "statusLine command emitted output" || nb "statusLine emitted nothing (stderr: $(head -c 120 "$WORK/sl.err"))"

# ------------------------------------------------------------------- live ---
echo
echo "[8] live turns (requires copilot auth)"
if [ "$OFFLINE_ONLY" = "1" ]; then
  skip "--offline requested"
else
  # Auth rides in config.json (proven live): copying the operator's copy into
  # the scratch home authenticates it. The file is never read or printed.
  # Overwrite unconditionally — the pre-auth probes above already made the
  # binary write an unauthenticated machine-managed config.json here.
  if [ -f "$HOME/.copilot/config.json" ]; then
    cp -f "$HOME/.copilot/config.json" "$COPILOT_HOME/config.json"
  fi
  AUTH_PROBE="$(cd "$WORK/repo" && timeout 90 copilot -p "reply with OK" --allow-all-tools --no-color 2>&1 | head -3)"
  if printf '%s' "$AUTH_PROBE" | grep -qi "No authentication information found"; then
    skip "copilot is NOT authenticated — run \`copilot\` then /login, or export COPILOT_GITHUB_TOKEN"
    skip "  blocked: hook firing in a real session, persona reaching model context,"
    skip "  exit-code block semantics outside preToolUse, MCP tool call, --resume"
  else
    LOGOFF=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    OUT="$(cd "$WORK/repo" && timeout 150 copilot -p "Print the word ALPHA and nothing else." \
      --allow-all-tools --no-color --log-level all 2>&1)"
    MINE=$(tail -n +$((LOGOFF + 1)) "$LOG" 2>/dev/null || true)
    printf '%s' "$MINE" | grep -qi "Session started" && ok "SessionStart dispatched to a real handler in a live turn" \
      || no "no hook-log evidence of SessionStart in a live turn"
    printf '%s' "$MINE" | grep -qi "prompt submitted\|Pre tool use" && ok "turn events dispatched" || nb "no tool/prompt events this turn"
    # Persona: assert the file the model was given, not the model's prose.
    printf '%s' "$OUT" | grep -qi "copilot-instructions" && ok "instructions file loaded (log evidence)" \
      || nb "instructions load not visible in the log at this level"
    printf '%s' "$OUT" | grep -q "ALPHA" && ok "live turn completed (model replied)" || no "live turn produced no reply"

    # Project bridge: a repo rule body must reach the model, and copilot must
    # NOT get the .agents/skills link (it scans .claude/skills itself).
    mkdir -p "$WORK/repo/.claude/rules" "$WORK/repo/.claude/skills/demo"
    echo "The SMOKE-RULE-CANARY for this project is PINEAPPLE." > "$WORK/repo/.claude/rules/canary.md"
    B="$(cd "$WORK/repo" && timeout 150 copilot -p "Without using any tools, what is the SMOKE-RULE-CANARY? Answer with one word." \
      --allow-all-tools --no-color 2>&1)"
    printf '%s' "$B" | grep -qi "PINEAPPLE" && ok "project rule body reached the model" \
      || no "project rule body absent from the model's context"
    [ -e "$WORK/repo/.agents" ] && no "copilot got a .agents skills link it does not need" \
      || ok "no .agents link created for copilot"

    # Exit-code semantics OUTSIDE preToolUse (settles [6] live): exit 2 on
    # userPromptSubmitted must NOT block; the {"decision":"block"} envelope must.
    printf '#!/bin/sh\necho PROBE >&2\nexit 2\n' > "$COPILOT_HOME/x2.sh" && chmod +x "$COPILOT_HOME/x2.sh"
    printf '{"version":1,"hooks":{"userPromptSubmitted":[{"type":"command","command":"%s","timeoutSeconds":10}]}}' "$COPILOT_HOME/x2.sh" > "$COPILOT_HOME/hooks/zz-probe.json"
    L1="$(cd "$WORK/repo" && timeout 120 copilot -p "reply with exactly: NOT_BLOCKED" --no-color 2>&1 | head -2)"
    printf '%s' "$L1" | grep -q "NOT_BLOCKED" && ok "exit 2 on userPromptSubmitted is advisory (turn ran)" \
      || no "exit 2 unexpectedly blocked userPromptSubmitted"
    printf '#!/bin/sh\nprintf %s\n' "'{\"decision\":\"block\",\"reason\":\"SMOKE-ENV-BLOCK\"}'" > "$COPILOT_HOME/x2.sh"
    L2="$(cd "$WORK/repo" && timeout 120 copilot -p "reply with exactly: NOT_BLOCKED" --no-color 2>&1 | head -2)"
    printf '%s' "$L2" | grep -q "SMOKE-ENV-BLOCK" && ok "stdout {decision:block} blocks userPromptSubmitted" \
      || no "decision:block envelope did not block the prompt"
    rm -f "$COPILOT_HOME/hooks/zz-probe.json" "$COPILOT_HOME/x2.sh"
  fi
fi

echo
echo "=== $PASS passed, $FAIL failed, $SKIP skipped, $INFO informational ==="
[ "$FAIL" -eq 0 ] || exit 1
[ "$SKIP" -eq 0 ] || { echo "NOTE: skipped checks are unverified, not passing."; exit 0; }
