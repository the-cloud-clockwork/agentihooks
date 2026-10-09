#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../../../.."
chart=deploy/helm/agentihooks-swarm
release=agentihooks-swarm
image=local/agentihooks-swarm:dev
slug=kind-proof
cluster="swarm-kind-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
ledger_pod="$release-ledger-0"
controller=(-l "app.kubernetes.io/instance=$release,app.kubernetes.io/component=controller")

finish() {
  status=$?
  if [[ $status -ne 0 ]]; then
    kubectl get pods -o wide || true
    kubectl logs "${controller[@]}" --tail 100 || true
    kubectl logs "$ledger_pod" --tail 100 || true
  fi
  kind delete cluster --name "$cluster" || true
  exit "$status"
}

helm lint --strict "$chart"
helm lint --strict "$chart" -f "$chart/ci/kind-values.yaml"
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
docker build -q -t "$image" . >/dev/null &
build=$!
trap finish EXIT
kind create cluster --name "$cluster" --wait 120s
wait "$build"
kind load docker-image "$image" --name "$cluster"
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
for _ in $(seq 300); do
  restarts="$(kubectl get pods "${controller[@]}" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')"
  if [[ $restarts == 1 ]]; then
    break
  fi
  sleep 1
done
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
printf 'Helm chart kind proof passed\n'
