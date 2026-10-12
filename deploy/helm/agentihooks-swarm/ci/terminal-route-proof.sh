#!/usr/bin/env bash
set -euo pipefail

image=$1
fixture=tests/fixtures/swarm_v2/herdr-terminal.json
objects=tests/fixtures/swarm_v2/herdr-terminal-objects.json
workers=swarm-workers
control=swarm-control
other=swarm-other

read_fixture() { python3 -c 'import functools, json, sys; value = functools.reduce(lambda found, key: found[key], sys.argv[2:], json.load(open(sys.argv[1]))); print(value if isinstance(value, str) else json.dumps(value))' "$fixture" "$@"; }
worker="$(read_fixture pod metadata name)"
worker_labels="$(read_fixture pod metadata labels)"
client_labels="$(read_fixture clients labels)"
host="${worker}-terminal.${workers}.svc.cluster.local"
missing_host="swarm-exe-$(printf 'b%.0s' $(seq 32))-terminal.${workers}.svc.cluster.local"

kubectl create namespace "$control"
kubectl create namespace "$other"

pod() {
  local name=$1 namespace=$2 labels=$3
  shift 3
  python3 - "$name" "$namespace" "$labels" "$image" "$@" <<'EOF' | kubectl create -f -
import json, sys
name, namespace, labels, image, *command = sys.argv[1:]
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": name, "namespace": namespace, "labels": json.loads(labels)},
    "spec": {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001},
        "containers": [{"name": "main", "image": image, "imagePullPolicy": "Never", "command": command}],
    },
}))
EOF
}
sleeper=(python -c "import time; time.sleep(3600)")
pod "$worker" "$workers" "$worker_labels" python -m http.server 2222
pod allowed "$control" "$client_labels" "${sleeper[@]}"
pod foreign-namespace "$other" "$client_labels" "${sleeper[@]}"
pod unlabelled "$control" '{}' "${sleeper[@]}"
for target in "$workers/$worker" "$control/allowed" "$other/foreign-namespace" "$control/unlabelled"; do
  kubectl wait --for=condition=Ready --timeout 120s -n "${target%%/*}" "pod/${target##*/}"
done

uid="$(kubectl get pod -n "$workers" "$worker" -o jsonpath='{.metadata.uid}')"
python3 - "$objects" "$uid" <<'EOF' | kubectl create -f -
import json, sys
rendered = json.load(open(sys.argv[1]))
for item in rendered["items"]:
    for owner in item["metadata"]["ownerReferences"]:
        owner["uid"] = sys.argv[2]
print(json.dumps(rendered))
EOF

probe='
import ipaddress, json, socket, sys
host = sys.argv[1]
try:
    found = socket.getaddrinfo(host, 2222, type=socket.SOCK_STREAM)
except OSError:
    found = []
addresses = sorted({info[4][0] for info in found})
if not addresses:
    print(json.dumps({"host": host, "refused": "no_route"}))
    raise SystemExit(0)
private = all(ipaddress.ip_address(address).is_private for address in addresses)
try:
    socket.create_connection((addresses[0], 2222), timeout=5).close()
    connected = True
except OSError:
    connected = False
print(json.dumps({"host": host, "address": addresses[0], "private": private, "connected": connected}))
'
reach() { kubectl exec -n "${1%%/*}" "${1##*/}" -- python -c "$probe" "$2"; }

allowed=""
for _ in $(seq 30); do
  allowed="$(reach "$control/allowed" "$host")"
  [[ $allowed == *'"private": true, "connected": true'* ]] && break
  sleep 2
done
if [[ $allowed != *'"private": true, "connected": true'* ]]; then
  printf 'the allowed client in %s could not reach the worker terminal over a private route: %s\n' "$control" "$allowed" >&2
  exit 1
fi
for client in "$other/foreign-namespace" "$control/unlabelled"; do
  denied=""
  for _ in $(seq 15); do
    denied="$(reach "$client" "$host")"
    [[ $denied == *'"connected": false'* ]] && break
    sleep 2
  done
  for _ in 1 2 3; do
    if [[ $denied != *'"connected": false'* ]]; then
      printf 'client %s reached the worker terminal outside its policy: %s\n' "$client" "$denied" >&2
      exit 1
    fi
    denied="$(reach "$client" "$host")"
  done
  printf 'worker terminal refused client %s: %s\n' "$client" "$denied"
done
missing="$(reach "$control/allowed" "$missing_host")"
if [[ $missing != *'"refused": "no_route"'* ]]; then
  printf 'an execution with no terminal Service resolved: %s\n' "$missing" >&2
  exit 1
fi
printf 'worker terminal route proof: the allowed client reached it over cluster DNS %s; an execution with no Service %s\n' "$allowed" "$missing"
