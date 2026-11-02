#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../../../.."
image=$1
release=$2
chart=deploy/helm/agentihooks-swarm
hive="$release-worker-hive"
ledger_pod="$release-ledger-0"
redis_url="redis://$release-redis:6379/7"
slug=kind-drain
task=drain1
drain=120
owner="default/$release"
lease=agentihooks-hive-rollout
logs="$(mktemp -d)"
follow=""
rolled=""
cleanup() {
  for job in "$follow" "$rolled"; do
    if [[ -n $job ]]; then
      kill "$job" 2>/dev/null || true
    fi
  done
  rm -rf "$logs"
}
trap cleanup EXIT
values=(
  --set workers.replicas=1 --set workers.maxAgents=1
  --set workers.memoryPerAgentMi.request=128 --set workers.memoryPerAgentMi.limit=512
  --set workers.rollout.enabled=true --set "workers.rollout.drainSeconds=$drain"
  --set "workers.image.repository=${image%:*}" --set "workers.image.tag=${image##*:}"
  --set workers.image.pullPolicy=Never
  --set workers.credentialSecret.name=kind-hive-drain-join --set workers.credentialSecret.key=credential
)

redis() { kubectl exec "$release-redis-0" -- redis-cli -n 7 "$@"; }
holder() { kubectl get lease "$lease" -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || true; }
rollout_objects() {
  helm template "$release" "$chart" "${values[@]}" --show-only templates/hive-rollout.yaml | python3 -c '
import sys, yaml
wanted = sys.argv[1]
docs = [doc for doc in yaml.safe_load_all(sys.stdin) if doc]
picked = [doc for doc in docs if (doc["kind"] == "Job") == (wanted != "rbac") and (wanted == "rbac" or doc["metadata"]["name"].endswith(wanted))]
if not picked:
    sys.exit(f"the chart rendered no rollout {wanted} objects")
yaml.safe_dump_all(picked, sys.stdout)
' "$1"
}
run_job() {
  rollout_objects "$1" | kubectl apply -f -
  if ! kubectl wait --for=condition=complete "job/$release-rollout-$1" --timeout 3m; then
    kubectl logs "job/$release-rollout-$1" --tail 50 || true
    printf 'the chart rollout %s job did not complete\n' "$1" >&2
    exit 1
  fi
}
apply_hive() {
  helm template "$release" "$chart" "${values[@]}" --set "workers.rollout.release=$1" \
    --show-only templates/worker-hive.yaml | python3 -c '
import sys, yaml
drain, redis_url, slug, task = sys.argv[1:]
docs = [doc for doc in yaml.safe_load_all(sys.stdin) if doc]
for doc in docs:
    if doc["kind"] != "Deployment":
        continue
    pod = doc["spec"]["template"]["spec"]
    container = pod["containers"][0]
    stop = " ".join(container["lifecycle"]["preStop"]["exec"]["command"])
    if f"agentihooks hive drain --wait {drain} >>" not in stop or pod["terminationGracePeriodSeconds"] != int(drain) + 30:
        sys.exit(f"the chart worker hive no longer drains for {drain} seconds before it stops: {stop}")
    if "readinessProbe" not in container:
        sys.exit("the chart worker hive has no rollout readiness probe")
    container["command"] = ["bash", "/opt/drain/hive-drain-agent.sh"]
    env = [item for item in container["env"] if item["name"] != "AGENTIHOOKS_DEPLOYMENT"]
    env += [
        {"name": "AGENTIHOOKS_DEPLOYMENT", "value": "local"},
        {"name": "AGENTIHOOKS_SWARM_REDIS_URL", "value": redis_url},
        {"name": "DRAIN_SLUG", "value": slug},
        {"name": "DRAIN_TASK", "value": task},
        {"name": "DRAIN_HANDOFF_DELAY_S", "value": "5"},
    ]
    container["env"] = env
    container["volumeMounts"].append({"name": "drain-agent", "mountPath": "/opt/drain", "readOnly": True})
    pod["volumes"].append({"name": "drain-agent", "configMap": {"name": "kind-hive-drain-agent"}})
yaml.safe_dump_all(docs, sys.stdout)
' "$drain" "$redis_url" "$slug" "$task" | kubectl apply -f -
  kubectl rollout status "deployment/$hive" --timeout 3m
}

printf 'kind-hive-drain-member' | kubectl create secret generic kind-hive-drain-join --from-file=credential=/dev/stdin
kubectl create configmap kind-hive-drain-agent --from-file="$chart/ci/hive-drain-agent.sh"
kubectl exec "$ledger_pod" -- env AGENTIHOOKS_DEPLOYMENT=local AGENTIHOOKS_SWARM_REDIS_URL="$redis_url" python -c \
  "from scripts.swarm.store import SwarmConfig, connect; connect().create(SwarmConfig('$slug', '/data', 0, 0, state='stopped'))"
rollout_objects rbac | kubectl apply -f -
apply_hive first

old=""
agent=""
for _ in $(seq 60); do
  old="$(redis GET "kind-drain:$slug:agent-hive" || true)"
  agent="$(redis GET "kind-drain:$slug:agent" || true)"
  [[ -n $old && -n $agent ]] && break
  sleep 1
done
if [[ -z $old || -z $agent ]]; then
  kubectl logs "deployment/$hive" --tail 50 || true
  printf 'the scripted agent never took its seat on the hive\n' >&2
  exit 1
fi
kubectl logs -f "$old" >"$logs/old.log" 2>&1 &
follow=$!
printf 'the scripted agent %s works %s on hive %s\n' "$agent" "$task" "$old"

run_job acquire
if [[ "$(holder)" != "$owner" ]]; then
  printf 'the chart rollout acquire job left the lease held by %s, not %s\n' "$(holder)" "$owner" >&2
  exit 1
fi
printf 'the roll lease is held by %s; rolling the hive\n' "$owner"
apply_hive second &
rolled=$!

samples=0
for _ in $(seq $((drain + 90))); do
  if ! kubectl get pod "$old" >/dev/null 2>&1; then
    break
  fi
  held="$(holder)"
  if [[ $held != "$owner" ]]; then
    printf 'the roll lease was held by %s while hive %s drained, not %s\n' "${held:-nobody}" "$old" "$owner" >&2
    exit 1
  fi
  samples=$((samples + 1))
  sleep 1
done
if kubectl get pod "$old" >/dev/null 2>&1; then
  printf 'hive %s never stopped\n' "$old" >&2
  exit 1
fi
if ! wait "$rolled"; then
  printf 'the replacement hive rollout did not finish\n' >&2
  exit 1
fi
rolled=""
wait "$follow" || true
follow=""
cat "$logs/old.log"

facts="$(kubectl exec "$ledger_pod" -- env AGENTIHOOKS_DEPLOYMENT=local AGENTIHOOKS_SWARM_REDIS_URL="$redis_url" python -c "
import json
from scripts.swarm.store import connect
store = connect()
print(json.dumps({
    'handed': json.loads(store.redis.get('kind-drain:$slug:handed') or 'null'),
    'stopped': store.redis.hget('kind-drain:$slug:stopped', '$old'),
    'envelope': store.handoff_envelope('$slug', '$task'),
}))
")"
python3 - "$facts" "$old" "$agent" "$samples" "$logs/old.log" <<'EOF'
import json
import sys
from pathlib import Path

facts, old, agent, samples, log = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
handed, stopped, envelope = facts["handed"], facts["stopped"], facts["envelope"] or {}
if handed is None:
    sys.exit(f"hive {old} stopped before {agent} recorded its own handoff; recorded envelope: {json.dumps(envelope)}")
if stopped is None:
    sys.exit(f"hive {old} recorded no stop")
if handed["hive"] != old or handed["at"] >= int(stopped):
    sys.exit(f"{agent} handed off at {handed['at']} on {handed['hive']}, not before hive {old} stopped at {stopped}")
if envelope.get("agent") != agent or envelope.get("reason") != "recycle":
    sys.exit(f"the recorded handoff is not {agent}'s own: {json.dumps(envelope)}")
if samples == 0:
    sys.exit(f"the roll lease was never read while hive {old} drained")
if f"hive {old} drained: every agent handed off, 1 ordered to hand off" not in Path(log).read_text():
    sys.exit(f"the preStop drain of hive {old} did not order {agent} and end on its handoff")
print(
    f"{agent} recorded its handoff {int(stopped) - handed['at']} ms before hive {old} stopped, "
    f"and the roll lease stayed held through {samples} drain readings"
)
EOF

run_job release
if [[ -n "$(holder)" ]]; then
  printf 'the chart rollout release job left the lease held by %s after the replacement hive served\n' "$(holder)" >&2
  exit 1
fi
printf 'the replacement hive serves and the release job freed the roll lease\n'
kubectl delete deployment "$hive" --wait=false
