#!/usr/bin/env bash
set -euo pipefail

image=$1
release=$2
slug=$3
namespace=swarm-pod-proof

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: swarm-refusal-proof
  namespace: $namespace
rules:
  - apiGroups: [""]
    resources: [pods]
    verbs: [create, get, delete]
  - apiGroups: [""]
    resources: [pods/log]
    verbs: [get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: swarm-refusal-proof
  namespace: $namespace
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: swarm-refusal-proof
subjects:
  - kind: ServiceAccount
    name: swarm-grant-proof
    namespace: default
EOF

refuse="$(cat <<'EOF'
import json, os, ssl, sys, tempfile, time
from pathlib import Path

from scripts.hive import auth as hive_auth
from scripts.swarm import commands
from scripts.swarm.store import AgentRecord, connect
from scripts.swarm_v2 import control_service
from scripts.swarm_v2.deployed import DEFAULT_EXPORTER
from scripts.swarm_v2.kubernetes.client import KubeHttp, PodClient
from scripts.swarm_v2.kubernetes.diagnostics import retain
from scripts.swarm_v2.kubernetes.grants import PROJECT_ANNOTATION, PodGrants, Supervision
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import pod_name
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, OWNER_LABEL, owner_for

access = json.load(sys.stdin)
env = dict(os.environ)
for line in Path(env["AGENTIHOOKS_HOME"], "controller", "credential.env").read_text().splitlines():
    name, _, value = line.partition("=")
    env[name] = value
store, slug, namespace = connect(), env["SLUG"], env["NAMESPACE"]
probe = control_service.ControlService(
    store,
    slug,
    control_service.launch_key(env),
    lambda: hive_auth.controller(store.redis, env[control_service.CREDENTIAL_ENV]),
    owner=commands.hive_id(),
)
assert probe.start(), "the probe could not share the deployed controller lease"
scratch = tempfile.TemporaryDirectory()
token_file = Path(scratch.name) / "token"
token_file.write_text(access["token"])
server = f"https://{env['KUBERNETES_SERVICE_HOST']}:{env['KUBERNETES_SERVICE_PORT']}"
api = PodClient(KubeHttp(server, token_file, ssl.create_default_context(cadata=access["ca"])), namespace)
record = AgentRecord(
    store.next_name(slug, "eng"),
    "eng",
    "kind-refusal",
    seat=f"eng-5@{slug}",
    runtime_backend=BACKEND,
    runtime_target={"pod_namespace": namespace, "pod_name": "swarm-kind-refusal"},
)
agent = probe.controller.admit(record, "")
grant = probe.grants.issue(
    slug, agent.execution_id, project_ids=["github.com/the-cloud-clockwork/agentihooks"], brain_id="swarm", account="kind"
)
name = pod_name(agent.execution_id)
api.create_pod({
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": name,
        "namespace": namespace,
        "labels": {
            OWNER_LABEL: owner_for(slug),
            EXECUTION_LABEL: agent.execution_id,
            GENERATION_LABEL: str(agent.generation),
        },
        "annotations": {PROJECT_ANNOTATION: "github.com/the-cloud-clockwork/refused-startup"},
    },
    "spec": {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001},
        "containers": [{
            "name": "worker",
            "image": env["IMAGE"],
            "imagePullPolicy": "Never",
            "command": ["python", "-c", "import sys; print('worker startup refused: planted kind refusal'); sys.exit(78)"],
        }],
    },
})
for _ in range(120):
    if (api.read_pod(name) or {}).get("status", {}).get("phase") == "Failed":
        break
    time.sleep(1)
else:
    raise SystemExit(f"the planted worker {name} never reached Failed")
retained = []


def keep(pod, captured):
    retain(store, slug, pod, captured)
    retained.append(pod["metadata"]["uid"])


hand = PodGrants(
    api,
    slug,
    lambda token: probe.grants.verify(slug, token),
    Supervision("claude", DEFAULT_EXPORTER, env["CONTROL_URL"]),
    retain=keep,
).hand(agent, grant)
scratch.cleanup()
print(json.dumps({"pod": name, "execution": agent.execution_id, "reason": hand.reason, "removed": hand.removed, "retained": len(retained)}))
EOF
)"
token="$(kubectl create token swarm-grant-proof --duration 10m)"
ca="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)"
refused="$(TOKEN="$token" CA="$ca" python3 -c 'import json, os; print(json.dumps({"token": os.environ["TOKEN"], "ca": os.environ["CA"]}))' \
  | kubectl exec -i "deployment/$release-controller" -c controller -- \
    env SLUG="$slug" NAMESPACE="$namespace" IMAGE="$image" CONTROL_URL="http://$release-controller:8780" \
    python -c "$refuse" | tail -1)"
unset token
if [[ $refused != *'"reason": "project_unpermitted"'* || $refused != *'"removed": true'* || $refused != *'"retained": 1'* ]]; then
  kubectl get pods -n "$namespace" -o wide || true
  kubectl logs "deployment/$release-controller" -c controller --tail 50 || true
  printf 'the launch cleanup did not remove the refused worker: %s\n' "$refused" >&2
  exit 1
fi
pod="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["pod"])' "$refused")"
execution="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["execution"])' "$refused")"
for _ in $(seq 60); do
  kubectl get pod -n "$namespace" "$pod" >/dev/null 2>&1 || break
  sleep 1
done
if kubectl get pod -n "$namespace" "$pod" >/dev/null 2>&1; then
  printf 'the refused worker %s still exists after cleanup\n' "$pod" >&2
  exit 1
fi
printf 'the refused worker %s is gone after launch cleanup\n' "$pod"
kept="$(kubectl exec "deployment/$release-controller" -c controller -- python -c 'import sys; from scripts.swarm.store import connect; store = connect(); kept = store.redis.hget(store.key(sys.argv[1], "attempt-startup-diagnostics"), sys.argv[2]); print(kept if isinstance(kept, str) else kept.decode())' "$slug" "$execution")"
python3 - "$kept" "$pod" <<'EOF'
import json
import sys

kept, pod = json.loads(sys.argv[1]), sys.argv[2]
container = kept["startup"]["containers"][0]
assert kept["pod"] == pod, kept
assert kept["startup"]["phase"] == "Failed", kept
assert container["terminated"]["exitCode"] == 78, container
assert container["terminated"]["reason"] == "Error", container
assert "worker startup refused: planted kind refusal" in container["log"], container
print(f"refused worker startup log and exit reason readable after cleanup: {json.dumps(kept, sort_keys=True)}")
EOF
