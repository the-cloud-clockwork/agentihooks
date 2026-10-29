#!/usr/bin/env bash
set -euo pipefail

# The fresh home receives the account only through the environment: NATIVE_PROOF_CLAUDE_ACCOUNT names an
# AH_CC_TOKEN_<slug> variable, NATIVE_PROOF_CODEX_ROUTE a Codex account slug, since a fresh home holds no quota readings.
# NATIVE_PROOF_IMAGE restores and resumes inside a fresh container of that image: only the durable store and this
# checkout are mounted, the restored home exists only inside the container, and the conversation reopens in the
# mounted project checkout.

proof_root="${1:?work directory required}"
harness="${2:?harness required: claude or codex}"
[[ "$harness" == "claude" || "$harness" == "codex" ]] || { echo "harness must be claude or codex" >&2; exit 2; }
proof_python="${NATIVE_PROOF_PYTHON:-python3}"
proof_repo="$(git rev-parse --show-toplevel)"
codeword="PLUM-$("$proof_python" -c 'import secrets; print(secrets.randbelow(9000) + 1000)')"

mkdir -p "$proof_root"/{source-home,source-cwd,fresh-home,fresh-cwd,durable}
proof_root="$(cd "$proof_root" && pwd)"
results="$proof_root/results.jsonl"
: > "$results"

record() {
    "$proof_python" -c 'import json, sys; print(json.dumps({"step": sys.argv[1], "exit": int(sys.argv[2]), "output": sys.argv[3]}))' \
        "$1" "$2" "$3" >> "$results"
}

run_cli() {
    local home="$1" cwd="$2"
    shift 2
    if [[ "$harness" == "claude" ]]; then
        local account="${NATIVE_PROOF_CLAUDE_ACCOUNT:?name the AH_CC_TOKEN_<slug> variable to use}"
        (cd "$cwd" && CLAUDE_CONFIG_DIR="$home" CLAUDE_CODE_OAUTH_TOKEN="${!account}" timeout 300 claude -p "$@")
    else
        local route="${NATIVE_PROOF_CODEX_ROUTE:?name the Codex account slug to route}"
        (cd "$cwd" && CODEX_HOME="$home" timeout 300 agentihooks codex --route "$route" exec --skip-git-repo-check "$@" < /dev/null)
    fi
}

tested="$(git -C "$proof_repo" rev-parse HEAD)"
git -C "$proof_repo" diff --quiet HEAD -- scripts tests || tested="$tested with uncommitted changes"
version="$("$harness" --version 2>&1)"
version="${version%%$'\n'*}"
record tested_commit 0 "$tested"
record installed_version 0 "$version"
if [[ -n "${NATIVE_PROOF_CLAUDE_ACCOUNT:-}${NATIVE_PROOF_CODEX_ROUTE:-}" ]]; then
    record account 0 "named by the operator environment, never written to the record or the carried material"
else
    record account 1 "no account named"
fi

set +e
told="$(run_cli "$proof_root/source-home" "$proof_root/source-cwd" "Remember the codeword $codeword. Reply only: ok" 2>&1)"
told_exit=$?
set -e
record source_session "$told_exit" "$(tail -c 400 <<< "$told")"
[[ "$told_exit" -eq 0 ]] || { echo "the source conversation did not start" >&2; exit 1; }

proof() {
    (cd "$proof_repo" && "$proof_python" -m tests.integration.native_resume.native_proof "$@")
}

question="What was the codeword I asked you to remember? Reply with the codeword only."

if [[ -z "${NATIVE_PROOF_IMAGE:-}" ]]; then
    carried="$(proof carry "$harness" "$proof_root/source-home" "$proof_root/source-cwd" \
        "$proof_root/fresh-home" "$proof_root/fresh-cwd" "$proof_root/durable" "$version")"
    record carry 0 "$carried"
    session="$("$proof_python" -c 'import json, sys; print(json.loads(sys.argv[1])["session_id"])' "$carried")"
    outcome="$("$proof_python" -c 'import json, sys; print(json.loads(sys.argv[1])["restore"]["outcome"])' "$carried")"
    [[ "$outcome" == "resumed" ]] || { echo "restore did not qualify: $outcome" >&2; exit 1; }
    set +e
    if [[ "$harness" == "claude" ]]; then
        answer="$(run_cli "$proof_root/fresh-home" "$proof_root/fresh-cwd" --resume "$session" "$question" 2>&1)"
    else
        answer="$(run_cli "$proof_root/fresh-home" "$proof_root/fresh-cwd" resume "$session" "$question" 2>&1)"
    fi
    answer_exit=$?
    set -e
    record fresh_home_resume "$answer_exit" "$(tail -c 400 <<< "$answer")"
else
    committed="$(proof commit "$harness" "$proof_root/source-home" "$proof_root/source-cwd" "$proof_root/durable" "$version")"
    record commit 0 "$committed"
    session="$("$proof_python" -c 'import json, sys; print(json.loads(sys.argv[1])["session_id"])' "$committed")"
    container_version="$(docker run --rm --entrypoint "$harness" "$NATIVE_PROOF_IMAGE" --version 2>/dev/null)"
    container_version="${container_version%%$'\n'*}"
    record container_version 0 "$container_version"
    inside='set -euo pipefail
mkdir -p "$HOME" /tmp/fresh-home
cd /src
python3 -m tests.integration.native_resume.native_proof restore "$1" /tmp/fresh-home /src /durable "$2" > /tmp/restore.json
cat /tmp/restore.json
python3 -c "import json, sys; sys.exit(json.load(open(\"/tmp/restore.json\"))[\"restore\"][\"outcome\"] != \"resumed\")"
if [[ "$1" == "claude" ]]; then
    CLAUDE_CONFIG_DIR=/tmp/fresh-home timeout 300 claude -p --resume "$3" "$4" < /dev/null
else
    CODEX_HOME=/tmp/fresh-home timeout 300 agentihooks codex --route "$5" exec --skip-git-repo-check resume "$3" "$4" < /dev/null
fi'
    passed=(-e HOME=/tmp/user -e PYTHONPATH=/src)
    if [[ "$harness" == "claude" ]]; then
        account="${NATIVE_PROOF_CLAUDE_ACCOUNT:?name the AH_CC_TOKEN_<slug> variable to use}"
        export CLAUDE_CODE_OAUTH_TOKEN="${!account}"
        passed+=(-e CLAUDE_CODE_OAUTH_TOKEN)
        route=""
    else
        route="${NATIVE_PROOF_CODEX_ROUTE:?name the Codex account slug to route}"
        token="AH_CX_TOKEN_$route"
        : "${!token:?the route $route has no token in the environment}"
        passed+=(-e "$token")
    fi
    set +e
    answer="$(docker run --rm --memory 1g --user "$(id -u):$(id -g)" --entrypoint bash "${passed[@]}" \
        -v "$proof_repo:/src:ro" -v "$proof_root/durable:/durable" "$NATIVE_PROOF_IMAGE" \
        -c "$inside" proof "$harness" "$container_version" "$session" "$question" "$route" 2>&1)"
    answer_exit=$?
    set -e
    answer="$(grep -v '^\[agentihooks codex\]' <<< "$answer" || true)"
    record fresh_container_resume "$answer_exit" "$(tail -c 1600 <<< "$answer")"
fi
grep -q "$codeword" <<< "$answer" && recalled=0 || recalled=1
record codeword_recalled "$recalled" "expected $codeword in the resumed answer"
exit "$recalled"
