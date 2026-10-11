#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../../../.."
chart=deploy/helm/agentihooks-swarm
release=agentihooks-swarm
image=local/agentihooks-swarm:dev
bundle_image=local/agentihooks-bundle:fixture
slug=kind-proof
cluster="swarm-kind-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
ledger_pod="$release-ledger-0"
controller=(-l "app.kubernetes.io/instance=$release,app.kubernetes.io/component=controller")
ca_dir=""

finish() {
  status=$?
  if [[ -n $ca_dir ]]; then
    rm -rf "$ca_dir"
  fi
  if [[ $status -ne 0 ]]; then
    kubectl get pods -o wide || true
    kubectl logs "${controller[@]}" --tail 100 || true
    kubectl logs "${controller[@]}" --previous --tail 100 || true
    kubectl get pods "${controller[@]}" -o jsonpath='{range .items[*].status.initContainerStatuses[*]}{.name} last state {.lastState}{"\n"}{end}{range .items[*].status.containerStatuses[*]}{.name} last state {.lastState}{"\n"}{end}' || true
    kubectl logs "$ledger_pod" --tail 100 || true
  fi
  kind delete cluster --name "$cluster" || true
  exit "$status"
}

helm lint --strict "$chart"
helm lint --strict "$chart" -f "$chart/ci/kind-values.yaml"
secrets=(-f "$chart/ci/kind-values.yaml")
helm template "$release" "$chart" "${secrets[@]}" | python3 tests/chart_secrets.py "$chart/ci/kind-values.yaml"
planted="$(helm template "$release" "$chart" "${secrets[@]}" --set 'secretEnv[0].name=AGENTIHOOKS_WORKER_TERMINAL' \
  --set 'secretEnv[0].secretName=swarm-worker-terminal' --set 'secretEnv[0].key=setting' \
  | python3 tests/chart_secrets.py "$chart/ci/kind-values.yaml" 2>&1 >/dev/null || true)"
if [[ $planted != *"StatefulSet $release-ledger container ledger references the controller only Secret swarm-worker-terminal"* ]]; then
  printf 'the chart check did not flag a planted shared reference to the controller only Secret: %s\n' "$planted" >&2
  exit 1
fi
planted="$(helm template "$release" "$chart" "${secrets[@]}" --set 'controller.secretEnv[0].optional=false' \
  | python3 tests/chart_secrets.py "$chart/ci/kind-values.yaml" 2>&1 >/dev/null || true)"
if [[ $planted != *"the controller container references swarm-worker-terminal as"* ]]; then
  printf 'the chart check did not flag a required reference where the values say optional: %s\n' "$planted" >&2
  exit 1
fi
digest="sha256:$(printf 'a%.0s' $(seq 64))"
rendered="$(helm template "$release" "$chart" --set image.tag="dev@$digest")"
if [[ $rendered != *":dev@$digest"* ]]; then
  printf 'the chart did not render the image updater tag dev@%s\n' "$digest" >&2
  exit 1
fi
for tag in "$digest" "@$digest" "$(printf 'b%.0s' $(seq 40))"; do
  refusal="$(helm template "$release" "$chart" --set image.tag="$tag" 2>&1 >/dev/null || true)"
  if [[ $refusal != *"must be a floating tag"* ]]; then
    printf 'the chart did not refuse image.tag %s as a non floating tag: %s\n' "$tag" "$refusal" >&2
    exit 1
  fi
done
refusal="$(helm template "$release" "$chart" --set ledger.replicas=2 2>&1 >/dev/null || true)"
if [[ $refusal != *"ledger.replicas must be 1"* ]]; then
  printf 'the chart did not refuse two ledger writers: %s\n' "$refusal" >&2
  exit 1
fi
refusal="$(helm template "$release" "$chart" --set controller.api.enabled=true 2>&1 >/dev/null || true)"
if [[ $refusal != *"controller.api.swarm must name the swarm"* ]]; then
  printf 'the chart did not refuse a control API without a swarm: %s\n' "$refusal" >&2
  exit 1
fi
refusal="$(helm template "$release" "$chart" --set controller.api.enabled=true --set controller.api.swarm="$slug" 2>&1 >/dev/null || true)"
if [[ $refusal != *"controller.api.signingKey needs keyId and secretName"* ]]; then
  printf 'the chart did not refuse a control API without a signing key: %s\n' "$refusal" >&2
  exit 1
fi
api=(
  --set controller.api.enabled=true
  --set controller.api.swarm="$slug"
  --set controller.api.signingKey.keyId=kind-launch
  --set controller.api.signingKey.secretName=swarm-launch-signing
)
workers=(-f "$chart/ci/kind-workers.yaml")
helm template "$release" "$chart" "${workers[@]}" "${api[@]}" | python3 tests/chart_workers.py "$chart/ci/kind-workers.yaml"
secrets+=("${workers[@]}" "${api[@]}")
both=("$chart/ci/kind-values.yaml" "$chart/ci/kind-workers.yaml")
helm template "$release" "$chart" "${secrets[@]}" | python3 tests/chart_secrets.py "${both[@]}"
planted="$(helm template "$release" "$chart" "${secrets[@]}" --set 'secretEnv[0].name=AGENTIHOOKS_TERMINAL_CA_KEY' \
  --set 'secretEnv[0].secretName=swarm-terminal-attach-ca' --set 'secretEnv[0].key=user_ca' \
  | python3 tests/chart_secrets.py "${both[@]}" 2>&1 >/dev/null || true)"
if [[ $planted != *"StatefulSet $release-ledger container ledger references the terminal CA Secret swarm-terminal-attach-ca"* ]]; then
  printf 'the chart check did not flag a planted ledger reference to the terminal CA Secret: %s\n' "$planted" >&2
  exit 1
fi
printf 'the chart check flagged a planted ledger reference to the terminal CA Secret\n'
policy_map=(-f "$chart/ci/kind-policy-configmap.yaml")
helm template "$release" "$chart" "${workers[@]}" "${policy_map[@]}" "${api[@]}" \
  | python3 tests/chart_workers.py "$chart/ci/kind-workers.yaml" "$chart/ci/kind-policy-configmap.yaml"
refuse_workers() {
  local expected=$1 refusal
  shift
  refusal="$(helm template "$release" "$chart" "${workers[@]}" "${api[@]}" "$@" 2>&1 >/dev/null || true)"
  if [[ $refusal != *"$expected"* ]]; then
    printf 'the chart did not refuse worker settings %s with "%s": %s\n' "$*" "$expected" "$refusal" >&2
    exit 1
  fi
}
for tag in "dev@$digest" "$digest" "$(printf 'b%.0s' $(seq 40))"; do
  refuse_workers "controller.workers.imageTag must be a floating tag" --set controller.workers.imageTag="$tag"
done
for setting in profile brain; do
  refuse_workers "controller.workers.$setting is required" --set controller.workers."$setting"=
done
refuse_workers "controller.workers.cap must be a whole number above zero" --set controller.workers.cap=0
refuse_workers "controller.workers.projects names no project" --set 'controller.workers.projects=null'
refuse_workers "controller.workers.podPolicy or podPolicyConfigMap is required" --set 'controller.workers.podPolicy=null'
refuse_workers "set controller.workers.podPolicy or podPolicyConfigMap, not both" \
  --set controller.workers.podPolicyConfigMap=swarm-pod-policy
refuse_workers "controller.serviceAccountName is required" --set controller.serviceAccountName=
for setting in secretName key; do
  refuse_workers "controller.terminalCa needs secretName and key with controller.workers.apiUrl" \
    --set controller.terminalCa."$setting"=
done
refuse_workers "controller.terminalCa needs secretName and key with controller.workers.apiUrl" \
  --set controller.terminalCa=null
printf 'the chart refused every incomplete or pinned worker setting\n'
docker build -q -t "$image" . >/dev/null &
build=$!
trap finish EXIT
kind create cluster --name "$cluster" --image "$KIND_NODE_IMAGE" --wait 120s
kubectl create namespace swarm-pod-proof
kubectl create serviceaccount swarm-worker --namespace swarm-pod-proof
printf 'apiVersion: node.k8s.io/v1\nkind: RuntimeClass\nmetadata:\n  name: kata-fc\nhandler: kata-fc\n' | kubectl create -f -
kubectl create --dry-run=server --validate=strict -f tests/fixtures/swarm_v2/pod-rendered.json
kubectl create --dry-run=server --validate=strict -f tests/fixtures/swarm_v2/pod-rendered-wrong-probes.json
kubectl create --dry-run=server --validate=strict -f tests/fixtures/swarm_v2/pod-rendered-bundle.json
printf 'rendered execution Pods passed server side strict validation\n'
kubectl create namespace swarm-workers
kubectl create --dry-run=server --validate=strict -f tests/fixtures/swarm_v2/herdr-terminal-pod.json
kubectl create --dry-run=server --validate=strict -f tests/fixtures/swarm_v2/herdr-terminal-objects.json
printf 'worker terminal Pod, Secret, Service and NetworkPolicy passed server side strict validation\n'
wait "$build"
bash scripts/packaging/swarm-image-clients.sh "$image"
printf 'FROM %s\nCOPY . /\n' "$image" \
  | docker build -q -t "$bundle_image" -f - tests/fixtures/swarm_v2/worker-bundle >/dev/null
kind load docker-image "$image" --name "$cluster"
kind load docker-image "$bundle_image" --name "$cluster"
bash "$chart/ci/terminal-route-proof.sh" "$image"
helm install "$release" "$chart" -f "$chart/ci/kind-values.yaml" --wait --timeout 5m
helm test "$release" --logs --timeout 2m

kubectl exec "$ledger_pod" -- env AGENTIHOOKS_DEPLOYMENT=local python -c "from scripts.swarm.store import SwarmConfig, connect; from scripts.swarm_ledger.new_ledger import create; create('$slug', {'title': 'Kind proof', 'phases': [{'title': 'Prove the Helm chart'}]}, 'swarm'); connect().create(SwarmConfig('$slug', '/data', 0, 0, state='paused')); print('ledger and swarm $slug registered, no lease bound')"
kubectl rollout restart deployment "$release-controller"
kubectl rollout status deployment "$release-controller" --timeout 2m
hive="$(kubectl get deployment "$release-controller" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SWARM_HIVE_ID")].value}')"
if [[ -z $hive ]]; then
  printf 'the controller deployment sets no SWARM_HIVE_ID\n' >&2
  exit 1
fi
read_lease() {
  kubectl exec "$ledger_pod" -- python -c "import json, dataclasses; from scripts.swarm import lease; from scripts.swarm.store import connect; held = lease.current(connect(), '$slug'); print(json.dumps(dataclasses.asdict(held) if held else {}))"
}
owner_of() { python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("owner", ""))' "$1"; }
expiry_of() { python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("expires_at", 0))' "$1"; }

held=""
first=""
for _ in $(seq 180); do
  held="$(read_lease)" || held="{}"
  if [[ "$(owner_of "$held")" == "$hive" ]]; then
    first="$held"
    break
  fi
  sleep 1
done
if [[ -z $first ]]; then
  printf 'the controller never took the lease for %s; last read: %s\n' "$slug" "$held" >&2
  exit 1
fi
printf 'controller lease: %s\n' "$first"

renewed=""
for _ in $(seq 180); do
  held="$(read_lease)" || held="{}"
  if [[ "$(owner_of "$held")" == "$hive" && "$(expiry_of "$held")" -gt "$(expiry_of "$first")" ]]; then
    renewed="$held"
    break
  fi
  sleep 1
done
if [[ -z $renewed ]]; then
  printf 'the controller never renewed the lease for %s; last read: %s\n' "$slug" "$held" >&2
  exit 1
fi
printf 'controller lease renewed: %s\n' "$renewed"

restarts="$(kubectl get pods "${controller[@]}" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')"
if [[ $restarts != 0 ]]; then
  printf 'the controller restarted %s times\n' "$restarts" >&2
  exit 1
fi
printf 'controller restarts: 0\n'

docker exec "$cluster-control-plane" pkill -STOP -f "agentihooks controller run"
printf 'controller frozen; its lease must lapse and the liveness probe must restart it\n'
kubectl wait pod "${controller[@]}" --for=jsonpath='{.status.containerStatuses[0].restartCount}'=1 --timeout 5m || true
restarts="$(kubectl get pods "${controller[@]}" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')"
if [[ $restarts != 1 ]]; then
  printf 'the liveness probe never restarted the frozen controller (restarts: %s)\n' "$restarts" >&2
  exit 1
fi
printf 'liveness probe restarted the frozen controller\n'
retaken=""
for _ in $(seq 180); do
  held="$(read_lease)" || held="{}"
  if [[ "$(owner_of "$held")" == "$hive" ]]; then
    retaken="$held"
    break
  fi
  sleep 1
done
if [[ -z $retaken ]]; then
  printf 'the restarted controller never took the lease back; last read: %s\n' "$held" >&2
  exit 1
fi
printf 'controller restarted by its liveness probe and holds the lease again: %s\n' "$retaken"

signing_key="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf '%s' "$signing_key" | kubectl create secret generic swarm-launch-signing --dry-run=client -o yaml \
  --from-file=signing-key=/dev/stdin | kubectl apply -f -
unset signing_key
kubectl create serviceaccount swarm-controller
kubectl create role swarm-controller --namespace swarm-pod-proof --verb=create,delete,get,list,watch --resource=pods
kubectl create rolebinding swarm-controller --namespace swarm-pod-proof --role=swarm-controller \
  --serviceaccount=default:swarm-controller
python3 -c 'import json, pathlib, sys, yaml; print(json.dumps(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())["controller"]["workers"]["podPolicy"]))' \
  "$chart/ci/kind-workers.yaml" | kubectl create configmap swarm-pod-policy --from-file=pod-policy.json=/dev/stdin \
  --dry-run=client -o yaml | kubectl apply -f -
ca_dir="$(mktemp -d)"
ssh-keygen -q -t ed25519 -N "" -C "kind user ca" -f "$ca_dir/user_ca"
ssh-keygen -q -t ed25519 -N "" -C "kind host ca" -f "$ca_dir/host_ca"
kubectl create secret generic swarm-terminal-attach-ca --from-file=user_ca="$ca_dir/user_ca" \
  --from-file=host_ca="$ca_dir/host_ca"
helm upgrade "$release" "$chart" -f "$chart/ci/kind-values.yaml" "${workers[@]}" "${policy_map[@]}" "${api[@]}" \
  --wait --timeout 5m
kubectl rollout status deployment "$release-controller" --timeout 2m
built=""
for _ in $(seq 60); do
  built="$(kubectl logs "deployment/$release-controller" -c controller | grep -F "controller: built the Kubernetes runtime for $slug" || true)"
  [[ -n $built ]] && break
  sleep 1
done
if [[ -z $built ]]; then
  printf 'the deployed controller never logged that it built the Kubernetes runtime for %s\n' "$slug" >&2
  exit 1
fi
printf 'the deployed controller built the Kubernetes runtime: %s\n' "$built"
registered="$(kubectl exec -i "deployment/$release-controller" -c controller -- env CONTROL_URL="http://$release-controller:8780" SLUG="$slug" python - <<'EOF'
import json, os, time, urllib.request
from pathlib import Path

from scripts.hive import auth as hive_auth
from scripts.swarm import commands
from scripts.swarm.store import AgentRecord, connect
from scripts.swarm_v2 import control_service
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime import observe

env = dict(os.environ)
for line in Path(env["AGENTIHOOKS_HOME"], "controller", "credential.env").read_text().splitlines():
    name, _, value = line.partition("=")
    env[name] = value
store, slug, base = connect(), env["SLUG"], env["CONTROL_URL"]
probe = control_service.ControlService(
    store,
    slug,
    control_service.launch_key(env),
    lambda: hive_auth.controller(store.redis, env[control_service.CREDENTIAL_ENV]),
    owner=commands.hive_id(),
)
assert probe.start(), "the probe could not share the deployed controller lease"
pod = {"pod_namespace": "default", "pod_name": "kind-worker"}
record = AgentRecord(
    store.next_name(slug, "eng"), "eng", "kind-task", seat=f"eng-1@{slug}", runtime_backend=BACKEND, runtime_target=pod
)
agent = probe.controller.admit(record, "")
token = probe.grants.issue(
    slug, agent.execution_id, project_ids=["github.com/the-cloud-clockwork/agentihooks"], brain_id="swarm", account="kind"
)


def send(path, body, method):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(base + path, json.dumps(body).encode(), headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


status, ack = send("/v2/executions/register", {"execution_id": agent.execution_id, "generation": agent.generation}, "POST")
beat = {
    "schema_version": "2.1",
    "operation_id": "kind-heartbeat-1",
    "authority": {
        "execution_id": agent.execution_id,
        "task_id": agent.task,
        "task_generation": ack["task_generation"],
        "controller_epoch": ack["controller_epoch"],
        "owner_identity": agent.name,
    },
    "renewal_sequence": 1,
    "state": "working",
    "observed_at": "2026-10-10T18:00:00Z",
}
beat_status, beat_ack = send(f"/v2/executions/{agent.execution_id}/heartbeat", beat, "PUT")
seen = None
for _ in range(60):
    seen = observe.stored(store, slug, agent.execution_id)
    if seen and "heartbeat" in seen.sources:
        break
    time.sleep(1)
print(json.dumps({
    "register": status,
    "heartbeat": beat_status,
    "renewal_sequence": beat_ack["renewal_sequence"],
    "observed": seen.state.value if seen else None,
}, sort_keys=True))
EOF
)"
if [[ $registered != *'"heartbeat": 200'* || $registered != *'"register": 200'* || $registered != *'"observed": "working"'* ]]; then
  printf 'a worker did not register, heartbeat and get observed through the deployed controller: %s\n' "$registered" >&2
  exit 1
fi
printf 'a worker registered and heartbeated against the deployed controller API: %s\n' "$registered"

kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: swarm-grant-proof
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: swarm-grant-proof
rules:
  - apiGroups: [""]
    resources: [pods]
    verbs: [create, get]
  - apiGroups: [""]
    resources: [configmaps]
    verbs: [create]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: swarm-grant-proof
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: swarm-grant-proof
subjects:
  - kind: ServiceAccount
    name: swarm-grant-proof
EOF
hand_grant="$(cat <<'EOF'
import json, os, ssl, sys, tempfile
from pathlib import Path

from scripts.hive import auth as hive_auth
from scripts.swarm import commands
from scripts.swarm.store import AgentRecord, connect
from scripts.swarm_v2 import control_service
from scripts.swarm_v2.filesystem import LAYOUTS
from scripts.swarm_v2.kubernetes.client import KubeHttp, PodClient
from scripts.swarm_v2.kubernetes.grants import PodGrants, Supervision
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import (
    ATTEMPTS,
    BUNDLE_CONTENT,
    BUNDLE_DIR,
    BUNDLE_VOLUME,
    LAUNCH_DIR,
    LAUNCH_RECORD,
    launch_volume,
    pod_name,
)
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.worker.start import TEMPLATES

access = json.load(sys.stdin)
env = dict(os.environ)
for line in Path(env["AGENTIHOOKS_HOME"], "controller", "credential.env").read_text().splitlines():
    name, _, value = line.partition("=")
    env[name] = value
store, slug = connect(), env["SLUG"]
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
api = PodClient(KubeHttp(server, token_file, ssl.create_default_context(cadata=access["ca"])), "default")
seed = (
    "import os, profiles, shutil; from pathlib import Path; "
    f"shutil.copytree(Path(profiles.__file__).parent, {str(TEMPLATES)!r}, dirs_exist_ok=True); "
    f"shutil.copytree(Path(profiles.__file__).parent / 'package' / 'roles', {str(TEMPLATES)!r}, dirs_exist_ok=True); "
    f"Path({str(LAYOUTS[1])!r}).write_text(os.environ['LAYOUT'])"
)


def launch(profile, seat):
    target = {"pod_namespace": "default", "pod_name": f"swarm-kind-{profile}"}
    record = AgentRecord(
        store.next_name(slug, "eng"),
        "eng",
        f"kind-{profile}",
        seat=f"{seat}@{slug}",
        runtime_backend=BACKEND,
        runtime_target=target,
        profile=profile,
    )
    agent = probe.controller.admit(record, "")
    grant = probe.grants.issue(
        slug, agent.execution_id, project_ids=["github.com/the-cloud-clockwork/agentihooks"], brain_id="swarm", account="kind"
    )
    name = pod_name(agent.execution_id)
    labels = {
        OWNER_LABEL: owner_for(slug),
        EXECUTION_LABEL: agent.execution_id,
        GENERATION_LABEL: str(agent.generation),
        "app.kubernetes.io/instance": env["RELEASE"],
    }
    api.create_pod({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "default", "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
            "initContainers": [{
                "name": "templates",
                "image": env["IMAGE"],
                "imagePullPolicy": "Never",
                "command": ["python", "-c", seed],
                "env": [{"name": "LAYOUT", "value": env["LAYOUT"]}],
                "volumeMounts": [
                    {"name": "templates", "mountPath": str(TEMPLATES.parent)},
                    {"name": "node", "mountPath": str(LAYOUTS[1].parent)},
                ],
            }, {
                "name": BUNDLE_VOLUME,
                "image": env["BUNDLE_IMAGE"],
                "imagePullPolicy": "Never",
                "command": ["cp", "-R", *BUNDLE_CONTENT, BUNDLE_DIR + "/"],
                "volumeMounts": [{"name": BUNDLE_VOLUME, "mountPath": BUNDLE_DIR}],
            }],
            "containers": [{
                "name": "worker",
                "image": env["IMAGE"],
                "imagePullPolicy": "Never",
                "args": ["python", "-m", "scripts.swarm_v2.worker.start", f"{ATTEMPTS}/{agent.execution_id}", f"{LAUNCH_DIR}/{LAUNCH_RECORD}"],
                "volumeMounts": [
                    {"name": "launch", "mountPath": LAUNCH_DIR, "readOnly": True},
                    {"name": "home", "mountPath": "/home/worker"},
                    {"name": "templates", "mountPath": str(TEMPLATES.parent), "readOnly": True},
                    {"name": "node", "mountPath": str(LAYOUTS[1].parent), "readOnly": True},
                    {"name": BUNDLE_VOLUME, "mountPath": BUNDLE_DIR, "readOnly": True},
                ],
            }],
            "volumes": [
                launch_volume(agent.execution_id),
                {"name": "home", "emptyDir": {}},
                {"name": "templates", "emptyDir": {}},
                {"name": "node", "emptyDir": {}},
                {"name": BUNDLE_VOLUME, "emptyDir": {}},
            ],
        },
    })
    hand = PodGrants(api, slug, lambda token: probe.grants.verify(slug, token), Supervision("claude", None, env["CONTROL_URL"])).hand(agent, grant)
    return {"pod": name, "profile": profile, "handed": hand.handed, "reason": hand.reason}


launched = [launch("engineer", "eng-2"), launch("frontend", "eng-3")]
scratch.cleanup()
for launched_pod in launched:
    print(json.dumps(launched_pod, sort_keys=True))
EOF
)"
handed="$(python3 - <<'EOF' | kubectl exec -i "deployment/$release-controller" -c controller -- env CONTROL_URL="http://$release-controller:8780" SLUG="$slug" RELEASE="$release" IMAGE="$image" BUNDLE_IMAGE="$bundle_image" LAYOUT="$(cat docker/swarm-node/layout.json)" python -c "$hand_grant"
import base64, json, subprocess


def run(*command):
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True).stdout


token = run("kubectl", "create", "token", "swarm-grant-proof", "--duration", "10m").strip()
ca = run("kubectl", "config", "view", "--raw", "--minify", "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}")
print(json.dumps({"token": token, "ca": base64.b64decode(ca).decode()}))
EOF
)"
declare -A chains=(
  [engineer]='"chains": {"claude": ["anton-base", "package:engineer", "engineer", "brain"]}'
  [frontend]='"chains": {"claude": ["anton-base", "package:engineer", "engineer", "frontend", "brain"]}'
)
launched="$(grep '^{"handed"' <<< "$handed" || true)"
if [[ $(grep -c '"handed": true' <<< "$launched") != 2 ]]; then
  printf 'the controller did not hand the launch grant to both worker Pods: %s\n' "$handed" >&2
  exit 1
fi
while read -r line; do
  worker_pod="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["pod"])' "$line")"
  profile="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["profile"])' "$line")"
  printf 'launch grant handed into the %s worker Pod launch ConfigMap: %s\n' "$profile" "$line"
  owner="$(kubectl get configmap "$worker_pod-launch" -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name} immutable={.immutable}')"
  if [[ $owner != "Pod/$worker_pod immutable=true" ]]; then
    printf 'the launch ConfigMap is not an immutable object owned by its Pod: %s\n' "$owner" >&2
    exit 1
  fi
  phase=""
  for _ in $(seq 120); do
    phase="$(kubectl get pod "$worker_pod" -o jsonpath='{.status.phase}')"
    [[ $phase == Succeeded || $phase == Failed ]] && break
    sleep 1
  done
  supervised="$(kubectl logs "$worker_pod" || true)"
  exit_code="$(kubectl get pod "$worker_pod" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}')"
  if [[ $phase != Succeeded && $phase != Failed ]] || [[ $exit_code == 64 ]] || grep -q '^ERROR .*refused' <<< "$supervised"; then
    kubectl describe pod "$worker_pod"
    kubectl logs "$worker_pod" -c templates || true
    kubectl logs "$worker_pod" -c bundle || true
    printf 'the %s worker Pod start step did not hand a prepared attempt to its supervisor (phase %s, exit %s): %s\n' "$profile" "$phase" "$exit_code" "$supervised" >&2
    exit 1
  fi
  execution="${worker_pod#swarm-}"
  prepared='{"worker_start": "prepared", "attempt": "'"$execution"'", "uid": 10001, '"${chains[$profile]}"'}'
  if [[ $supervised != *"$prepared"* || $supervised != *'"execution_id": "'"$execution"'"'* || $supervised != *'"supervisor_pid":'* ]]; then
    printf 'the %s worker did not render its bundle chain and run its supervisor against the registered authority: %s\n' "$profile" "$supervised" >&2
    exit 1
  fi
  printf 'the %s worker Pod rendered its bundle chain and started its supervisor without refusal (exit %s): %s\n' "$profile" "$exit_code" "$supervised"
  kubectl delete pod "$worker_pod" --wait --timeout 1m
  collected=""
  for _ in $(seq 60); do
    remaining="$(kubectl get configmap "$worker_pod-launch" --ignore-not-found -o name)"
    if [[ -z $remaining ]]; then
      collected=yes
      break
    fi
    sleep 1
  done
  if [[ -z $collected ]]; then
    printf 'the launch ConfigMap outlived its Pod\n' >&2
    exit 1
  fi
  printf 'the launch ConfigMap was collected with its Pod\n'
done <<< "$launched"

node="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
kubectl label node "$node" anton.io/capacity-type=spot --overwrite
kubectl rollout restart deployment "$release-controller"
placement=""
for _ in $(seq 60); do
  placement="$(kubectl get events --field-selector reason=FailedScheduling -o jsonpath='{range .items[*]}{.involvedObject.name} {.message}{"\n"}{end}' | grep "$release-controller" || true)"
  [[ $placement == *"node affinity"* ]] && break
  sleep 1
done
pending="$(kubectl get pods "${controller[@]}" --field-selector status.phase=Pending -o name)"
if [[ $placement != *"node affinity"* || -z $pending ]]; then
  printf 'the controller was not held off a node labelled as reclaimable capacity: %s\n' "$placement" >&2
  exit 1
fi
printf 'controller held Pending on reclaimable capacity: %s\n' "$(tail -1 <<< "$placement")"
restart_at="$(date +%s.%N)"
kubectl label node "$node" anton.io/capacity-type-
kubectl rollout status deployment "$release-controller" --timeout 2m
recovered="$(python3 -c 'import sys, time; print(round(time.time() - float(sys.argv[1]), 1))' "$restart_at")"
printf 'swarm_control_restart_recovery_seconds=%s measured from durable capacity returning to the control API ready (kind)\n' "$recovered"

before="$(kubectl get pods "${controller[@]}" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')"
host="$(kubectl get pods "${controller[@]}" -o jsonpath='{.items[0].metadata.name}')"
calm="$(kubectl exec "$ledger_pod" -- python -c "from scripts.swarm.store import PREFIX, connect; print(connect().redis.hget(f'{PREFIX}:host:$host:incident:pressure', 'active') or '0')")"
if [[ $calm != "0" ]]; then
  printf 'the host pressure alert was already active before the simulated pressure\n' >&2
  exit 1
fi
kubectl exec "$ledger_pod" -- python -c "from scripts.swarm.store import connect; connect().update('$slug', memory_per_agent_mb=10**9); print('simulated host pressure: one agent now needs more memory than the node has')"
alert=""
for _ in $(seq 120); do
  alert="$(kubectl exec "$ledger_pod" -- python -c "import json; from scripts.swarm.store import PREFIX, connect; root = f'{PREFIX}:host:$host:incident:pressure'; redis = connect().redis; print(json.dumps({'incident': redis.hgetall(root), 'mailed': {k: redis.hgetall(k) for k in redis.scan_iter(root + ':mail:*')}}, sort_keys=True))")" || alert=""
  if [[ $alert == *'"active": "1"'* && $alert == *'"raised": "1"'* ]]; then
    break
  fi
  alert=""
  sleep 1
done
if [[ -z $alert ]]; then
  printf 'the controller never sent the host pressure alert\n' >&2
  exit 1
fi
after="$(kubectl get pods "${controller[@]}" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')"
if [[ $after != "$before" ]]; then
  printf 'the controller restarted while sending the host pressure alert (%s then %s)\n' "$before" "$after" >&2
  exit 1
fi
printf 'controller sent the host pressure alert without a restart: %s\n' "$alert"

mounted="$(kubectl exec "deployment/$release-controller" -c controller -- sh -c \
  'ls /run/secrets/terminal-ca && stat -L -c "%a %u:%g" "$AGENTIHOOKS_TERMINAL_CA_KEY" && echo "$AGENTIHOOKS_TERMINAL_CA_KEY"')"
# fsGroup adds group read to a Secret file, so 0400 lands as 440 owned by root and the controller's group.
if [[ $mounted != $'user_ca\n440 0:10001\n/run/secrets/terminal-ca/user_ca' && $mounted != $'user_ca\n400 '*$'\n/run/secrets/terminal-ca/user_ca' ]]; then
  printf 'the controller does not hold only the user CA key, read only, at its setting path: %s\n' "$mounted" >&2
  exit 1
fi
printf 'the controller holds only the user CA key at its setting path: %s\n' "${mounted//$'\n'/ }"
python3 -c 'import json, sys; print(json.dumps({"clients": {"namespace": "default", "labels": {"app.kubernetes.io/instance": sys.argv[2], "app.kubernetes.io/component": "controller"}}, "user_ca": open(sys.argv[1]).read().strip()}))' \
  "$ca_dir/user_ca.pub" "$release" | kubectl create secret generic swarm-worker-terminal --from-file=setting=/dev/stdin
kubectl rollout restart deployment "$release-controller"
kubectl rollout status deployment "$release-controller" --timeout 2m
kept=""
for _ in $(seq 60); do
  kept="$(kubectl logs "deployment/$release-controller" -c controller | grep -F "controller: kept saved herdr machines for $slug on start" || true)"
  [[ -n $kept ]] && break
  sleep 1
done
if [[ -z $kept ]]; then
  printf 'the controller never logged its saved herdr machines on start with the terminal CA key mounted\n' >&2
  exit 1
fi
printf 'the controller loaded the mounted terminal CA key and logged its saved herdr machines on start: %s\n' "$kept"
if [[ $kept != *"refused {}"* ]]; then
  printf 'the controller start pass did not reach the herdr client: %s\n' "$kept" >&2
  exit 1
fi
printf 'the controller start pass reached the herdr client and refused nothing\n'
bash "$chart/ci/terminal-attach-proof.sh" "$image" "$cluster" "$release" "$slug"
printf 'Helm chart kind proof passed\n'
