#!/usr/bin/env bash
set -euo pipefail

worker_image=$1
release=$2
slug=$3
namespace=swarm-pod-proof
controller=(-l "app.kubernetes.io/instance=$release,app.kubernetes.io/component=controller")

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: swarm-terminal-proof
  namespace: $namespace
rules:
  - apiGroups: [""]
    resources: [pods]
    verbs: [create, get]
  - apiGroups: [""]
    resources: [secrets, services]
    verbs: [create]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies]
    verbs: [create]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: swarm-terminal-proof
  namespace: $namespace
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: swarm-terminal-proof
subjects:
  - kind: ServiceAccount
    name: swarm-grant-proof
    namespace: default
EOF

open_terminal="$(cat <<'EOF'
import json, os, ssl, sys, tempfile
from pathlib import Path

from scripts.hive import auth as hive_auth
from scripts.swarm import commands
from scripts.swarm.store import AgentRecord, connect
from scripts.swarm_v2 import control_service, deployed
from scripts.swarm_v2.herdr.endpoints import Endpoint, grant_scope, with_terminal
from scripts.swarm_v2.kubernetes.client import KubeHttp, PodClient
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import pod_name
from scripts.swarm_v2.kubernetes.terminal import HostKeys, TerminalObjects
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
    "kind-terminal",
    seat=f"eng-4@{slug}",
    runtime_backend=BACKEND,
    runtime_target={"pod_namespace": namespace, "pod_name": "swarm-kind-terminal"},
)
agent = probe.controller.admit(record, "")
grant = probe.grants.issue(
    slug, agent.execution_id, project_ids=["github.com/the-cloud-clockwork/agentihooks"], brain_id="swarm", account="kind"
)
registration = probe.grants.verify(slug, grant)
name = pod_name(agent.execution_id)
pod = {
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
    },
    "spec": {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
        "containers": [{
            "name": "worker",
            "image": env["WORKER_IMAGE"],
            "imagePullPolicy": "Never",
            "env": [
                {"name": "EXECUTION_ID", "value": agent.execution_id},
                {"name": "GENERATION", "value": str(agent.generation)},
                {"name": "SWARM", "value": slug},
            ],
            "volumeMounts": [],
        }],
        "volumes": [],
    },
}
api.create_pod(with_terminal(pod, Endpoint(grant_scope(registration), namespace), enabled=True))
exposure = deployed._terminal(env[deployed.TERMINAL_ENV])
refused = TerminalObjects(api, exposure, HostKeys(store)).open(registration, api.read_pod(name))
scratch.cleanup()
print(json.dumps({"pod": name, "execution": agent.execution_id, "agent": agent.name, "generation": agent.generation, "refused": refused}))
EOF
)"
token="$(kubectl create token swarm-grant-proof --duration 10m)"
ca="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)"
opened="$(TOKEN="$token" CA="$ca" python3 -c 'import json, os; print(json.dumps({"token": os.environ["TOKEN"], "ca": os.environ["CA"]}))' \
  | kubectl exec -i "deployment/$release-controller" -c controller -- \
    env SLUG="$slug" NAMESPACE="$namespace" WORKER_IMAGE="$worker_image" python -c "$open_terminal" | tail -1)"
unset token
if [[ $opened != *'"refused": ""'* ]]; then
  printf 'the controller could not open a terminal endpoint for the worker attempt: %s\n' "$opened" >&2
  exit 1
fi
worker_pod="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["pod"])' "$opened")"
printf 'terminal endpoint opened and host key recorded for the worker attempt: %s\n' "$opened"
ready=""
for _ in $(seq 120); do
  ready="$(kubectl logs -n "$namespace" "$worker_pod" 2>/dev/null | grep -F 'terminal worker ready' || true)"
  [[ -n $ready ]] && break
  sleep 1
done
if [[ -z $ready ]]; then
  kubectl describe pod -n "$namespace" "$worker_pod" || true
  kubectl logs -n "$namespace" "$worker_pod" || true
  printf 'the worker attempt never ran its sshd and herdr server\n' >&2
  exit 1
fi
printf '%s\n' "$ready"

kubectl rollout restart deployment "$release-controller"
kubectl rollout status deployment "$release-controller" --timeout 2m
attached=""
for _ in $(seq 120); do
  attached="$(kubectl logs "deployment/$release-controller" -c controller | grep -F "controller: kept saved herdr machines for $slug on start" || true)"
  [[ -n $attached ]] && break
  sleep 1
done
if [[ $attached != *"registered 1, refused {}"* ]]; then
  kubectl logs "${controller[@]}" -c controller --tail 50 || true
  kubectl logs -n "$namespace" "$worker_pod" || true
  kubectl exec -n "$namespace" "$worker_pod" -- cat /home/worker/.terminal/sshd.log || true
  printf 'the controller start pass did not attach to the worker attempt: %s\n' "$attached" >&2
  exit 1
fi
printf 'the controller start pass attached to the worker attempt over ssh and qualified its herdr server: %s\n' "$attached"
execution="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["execution"])' "$opened")"
machines="$(kubectl exec "deployment/$release-controller" -c controller -- herdr machine list --json)"
if [[ $machines != *"$execution"* ]]; then
  printf 'the controller saved no herdr machine for the worker attempt %s: %s\n' "$execution" "$machines" >&2
  exit 1
fi
printf 'the controller saved the worker attempt as a herdr machine: %s\n' "$machines"
agent="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["agent"])' "$opened")"
bash "$(dirname "$0")/terminal-identity-proof.sh" "$release" "$slug" "$execution" "$agent"
