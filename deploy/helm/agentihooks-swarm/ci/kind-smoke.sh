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
docker build -q -t "$image" . >/dev/null
kind create cluster --name "$cluster" --wait 120s
trap finish EXIT
kind load docker-image "$image" --name "$cluster"
helm install "$release" "$chart" -f "$chart/ci/kind-values.yaml" --wait --timeout 5m
helm test "$release" --logs --timeout 2m

kubectl exec "$ledger_pod" -- python -c "from scripts.swarm.store import SwarmConfig, connect; connect().create(SwarmConfig('$slug', '/data', 0, 0, state='paused')); print('swarm $slug registered, no lease bound')"
hive="$(kubectl get deployment "$release-controller" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SWARM_HIVE_ID")].value}')"
read_lease() {
  kubectl exec "$ledger_pod" -- python -c "import json, dataclasses; from scripts.swarm import lease; from scripts.swarm.store import connect; held = lease.current(connect(), '$slug'); print(json.dumps(dataclasses.asdict(held) if held else {}))"
}
owner_of() { python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("owner", ""))' "$1"; }
expiry_of() { python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("expires_at", 0))' "$1"; }

held=""
first=""
for _ in $(seq 60); do
  held="$(read_lease)"
  if [[ "$(owner_of "$held")" == "$hive" ]]; then
    first="$held"
    break
  fi
  sleep 3
done
if [[ -z $first ]]; then
  printf 'the controller never took the lease for %s; last read: %s\n' "$slug" "$held" >&2
  exit 1
fi
printf 'controller lease: %s\n' "$first"

renewed=""
for _ in $(seq 60); do
  held="$(read_lease)"
  if [[ "$(owner_of "$held")" == "$hive" && "$(expiry_of "$held")" -gt "$(expiry_of "$first")" ]]; then
    renewed="$held"
    break
  fi
  sleep 3
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
printf 'Helm chart kind proof passed\n'
