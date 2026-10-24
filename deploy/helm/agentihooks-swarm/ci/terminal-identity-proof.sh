#!/usr/bin/env bash
set -euo pipefail

release=$1
slug=$2
execution=$3
agent=$4
controller=("deployment/$release-controller" -c controller)

master="$(kubectl exec "${controller[@]}" -- env SLUG="$slug" python -c '
import os
from scripts.swarm.store import connect
print(connect().next_name(os.environ["SLUG"], "master"))' | tail -1)"

saved="$(kubectl exec "${controller[@]}" -- python -c '
import dataclasses, json, os
from scripts.swarm.store import connect
from scripts.swarm_v2.herdr.capabilities import runner
from scripts.swarm_v2.herdr.operator_service import Service
from scripts.swarm_v2.herdr.upkeep import Upkeep
from scripts.swarm_v2.kubernetes import operator_terminal
home = operator_terminal.client_home(os.environ)
terminal = operator_terminal.terminal(os.environ, home, runner(home.environment(dict(os.environ))))
print(json.dumps(dataclasses.asdict(Service(Upkeep(terminal), connect).run_pass())))' 2>&1 | tail -1)"
if ! python3 -c 'import json, sys; sys.exit(sys.argv[2] not in json.loads(sys.argv[1])["registered"])' "$saved" "$execution"; then
  printf 'the operator service did not save the controller bridge publication of %s: %s\n' "$execution" "$saved" >&2
  exit 1
fi
printf 'the operator service saved the controller bridge publication of %s: %s\n' "$execution" "$saved"

run="$(cat <<'EOF'
import json, os, sys

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, connect
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.operator_auth import OPERATOR

if os.environ.get("PLANT") == "every caller is the operator":
    operator_terminal.principal = lambda *_: OPERATOR
if seat := os.environ.get("SEAT"):
    store = connect()
    store.put_agent(sys.argv[1], AgentRecord(seat, "master", "master", seat=f"master@{sys.argv[1]}"))
    seated = [(agent.lane, agent.state) for agent in store.agents(sys.argv[1]) if agent.name == seat]
    print(f"seated {seat} as the master of {sys.argv[1]}: {json.dumps(seated)}", file=sys.stderr, flush=True)
sys.exit(cli.main([sys.argv[1], "terminal", sys.argv[2]]))
EOF
)"

terminal() {
  local fault=$1
  shift
  kubectl exec "${controller[@]}" -- env -u AGENTIHOOKS_AGENT_NAME -u AGENTIHOOKS_SWARM "$@" PLANT="$fault" \
    python -c "$run" "$slug" "$execution" 2>&1
}

check() {
  local fault=$1 out
  if ! out="$(terminal "$fault")"; then
    printf 'the operator identity could not attach to the worker terminal %s: %s\n' "$execution" "$out"
    return 1
  fi
  if [[ $out != *"\"incarnation\": \"$execution/"* ]]; then
    printf 'the operator identity attach named no incarnation of %s: %s\n' "$execution" "$out"
    return 1
  fi
  printf 'the operator identity attached to the worker terminal: %s\n' "$(tail -1 <<< "$out")"
  if out="$(terminal "$fault" SEAT="$master" AGENTIHOOKS_AGENT_NAME="$master" AGENTIHOOKS_SWARM="$slug")"; then
    printf 'the master identity %s was not refused the worker terminal: %s\n' "$master" "$out"
    return 1
  fi
  if [[ $out != *"only the operator role opens a worker terminal"* ]]; then
    printf 'the master identity %s was refused for another reason: %s\n' "$master" "$out"
    return 1
  fi
  printf 'the master identity was refused: %s\n' "$(grep -m1 '^swarm: ' <<< "$out")"
  if out="$(terminal "$fault" AGENTIHOOKS_AGENT_NAME="$agent" AGENTIHOOKS_SWARM="$slug")"; then
    printf 'the agent identity %s was not refused the worker terminal: %s\n' "$agent" "$out"
    return 1
  fi
  if [[ $out != *"only the operator or the master of swarm $slug uses its swarm controls"* ]]; then
    printf 'the agent identity %s was refused for another reason: %s\n' "$agent" "$out"
    return 1
  fi
  printf 'the agent identity was refused: %s\n' "$(grep -m1 '^swarm: ' <<< "$out")"
}

if ! check ""; then
  exit 1
fi
if planted="$(check "every caller is the operator")"; then
  printf 'the identity check passed with every caller resolved to the operator: %s\n' "$planted" >&2
  exit 1
fi
if [[ $planted != *"the master identity $master was not refused the worker terminal"* ]]; then
  printf 'the identity check missed the planted operator fallback for the master: %s\n' "$planted" >&2
  exit 1
fi
printf 'the identity check flagged the planted operator fallback: %s\n' "$(grep -m1 'was not refused' <<< "$planted")"
