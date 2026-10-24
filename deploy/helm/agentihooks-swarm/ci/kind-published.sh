#!/usr/bin/env bash
set -euo pipefail

version="${1:?usage: kind-published.sh VERSION}"
version="${version#v}"
owner="${GHCR_OWNER:-the-cloud-clockwork}"
chart="oci://ghcr.io/$owner/charts/agentihooks-swarm"
image="ghcr.io/$owner/agentihooks-swarm:$version"
release=agentihooks-swarm
cluster="swarm-published-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
pulled="$(mktemp -d)"
cluster_setup=""
ledger_tls=""

finish() {
  status=$?
  if [[ -n $cluster_setup ]]; then
    wait "$cluster_setup" || true
    if [[ $status -ne 0 ]]; then
      kubectl get pods -o wide || true
      kubectl logs "$release-ledger-0" --tail 100 || true
    fi
    kind delete cluster --name "$cluster" || true
  fi
  rm -rf "$pulled" "$ledger_tls"
  exit "$status"
}
trap finish EXIT

if ! helm pull "$chart" --version "$version" --destination "$pulled"; then
  printf 'the chart %s version %s is not published\n' "$chart" "$version" >&2
  exit 1
fi
archive="$pulled/agentihooks-swarm-$version.tgz"
if [[ ! -f $archive ]] || [[ "$(helm show chart "$archive" | sed -n 's/^version: //p')" != "$version" ]]; then
  printf 'the pulled chart is not agentihooks-swarm version %s\n' "$version" >&2
  exit 1
fi
tar -xzf "$archive" -C "$pulled"
printf 'pulled %s version %s\n' "$chart" "$version"

kind create cluster --name "$cluster" --image "$KIND_NODE_IMAGE" &
cluster_setup=$!
deadline=$((SECONDS + ${IMAGE_WAIT_SECONDS:-1200}))
until docker pull -q "$image"; do
  if ((SECONDS >= deadline)); then
    printf 'the image %s was not published in time\n' "$image" >&2
    exit 1
  fi
  sleep 20
done
wait "$cluster_setup"
kind load docker-image "$image" --name "$cluster"
kubectl wait --for=condition=Ready node --all --timeout=120s

ledger_tls="$(mktemp -d)"
ledger_host="$release-ledger"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$ledger_host" \
  -addext "subjectAltName=DNS:$ledger_host,DNS:$ledger_host.default,DNS:$ledger_host.default.svc,DNS:$ledger_host.default.svc.cluster.local,DNS:localhost,IP:127.0.0.1" \
  -keyout "$ledger_tls/tls.key" -out "$ledger_tls/tls.crt" 2>/dev/null
kubectl create secret generic swarm-ledger-tls --from-file="$ledger_tls/tls.crt" --from-file="$ledger_tls/tls.key" \
  --from-file=ca.crt="$ledger_tls/tls.crt"

helm install "$release" "$archive" -f "$pulled/agentihooks-swarm/ci/kind-values.yaml" \
  --set image.repository="${image%:*}" --set image.tag="$version" --set image.pullPolicy=Never \
  --wait --timeout 5m
helm test "$release" --logs --timeout 2m
printf 'the published chart %s version %s installed and passed helm test on kind\n' "$chart" "$version"
