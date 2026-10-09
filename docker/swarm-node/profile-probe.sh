#!/usr/bin/env bash
set -euo pipefail

attempts=/home/worker/attempts
mkdir -p "$attempts"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

boot() {
    python -m scripts.swarm_v2.worker_home bootstrap --templates /fixtures \
        --account claude=AH_CC_TOKEN_FIXTURE --account codex=AH_CX_TOKEN_FIXTURE \
        --endpoint AGENTIHOOKS_LEDGER_URL=http://ledger.swarm.invalid:8765 "$@"
}

pair() {
    local attempt="$1"
    shift
    boot --attempt "$attempt" --profile claude=fixture-claude --profile codex=fixture-codex "$@"
}

case "$1" in
positive)
    pair a1 > /tmp/record.json
    HOME="$attempts/a1/homes/claude" timeout 120 claude mcp list > /tmp/claude-mcp.txt 2>&1
    HOME="$attempts/a1/homes/codex" timeout 120 codex mcp list > /tmp/codex-mcp.txt 2>&1
    python /opt/probe/profile_probe.py positive "$attempts/a1"
    ;;
rejection)
    pair a0 > /dev/null
    python /opt/probe/profile_probe.py snapshot "$attempts" > /tmp/before.json
    set +e
    boot --attempt a1 --profile claude=fixture-workstation --profile codex=fixture-codex 2> /tmp/workstation.err
    echo $? > /tmp/workstation.exit
    pair a2 --interpreter /home/operator/dev/tcc-ecosystem/.venv/bin/python 2> /tmp/interpreter.err
    echo $? > /tmp/interpreter.exit
    set -e
    python /opt/probe/profile_probe.py snapshot "$attempts" > /tmp/after.json
    python /opt/probe/profile_probe.py rejection "$attempts"
    ;;
recovery)
    pair clean > /dev/null
    pair a1 > /tmp/first.json
    python /opt/probe/profile_probe.py snapshot "$attempts/a1" > /tmp/before.json
    pair a1 > /tmp/second.json
    python /opt/probe/profile_probe.py snapshot "$attempts/a1" > /tmp/after.json
    set +e
    python /opt/probe/profile_probe.py crash "$attempts" a2
    echo $? > /tmp/crash.exit
    set -e
    test -f "$attempts/a2/.bootstrap-pending"
    pair a2 > /tmp/restarted.json
    python /opt/probe/profile_probe.py recovery "$attempts"
    ;;
noexec)
    set +e
    pair a1 2> /tmp/noexec.err
    echo $? > /tmp/noexec.exit
    set -e
    python /opt/probe/profile_probe.py noexec "$attempts"
    ;;
esac
