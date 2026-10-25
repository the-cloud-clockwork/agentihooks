#!/usr/bin/env bash
set -euo pipefail

cluster="$(python3 -c 'import uuid; print("worker-hive-" + uuid.uuid4().hex[:12])')"
image=local/agentihooks-hive-worker:dev
out="$HOME/scratchpad/worker-hive-proof"
mkdir -p "$out"
cleanup() {
  status=$?
  if [[ "$status" != 0 ]]; then
    kubectl describe pods > "$out/pods.txt" 2>&1 || true
    kubectl logs -l app.kubernetes.io/component=worker-hive --tail=100 > "$out/failed-worker.txt" 2>&1 || true
    kubectl logs -l app.kubernetes.io/component=worker-hive --previous --tail=100 > "$out/previous-worker.txt" 2>&1 || true
    kubectl logs deployment/hive-join --tail=100 > "$out/join.txt" 2>&1 || true
    kubectl logs deployment/hive-join --previous --tail=100 > "$out/previous-join.txt" 2>&1 || true
  fi
  kind delete cluster --name "$cluster"
}
trap cleanup EXIT
kind create cluster --name "$cluster" --image mirror.gcr.io/kindest/node:v1.37.0 --wait 120s > "$out/cluster.log" 2>&1 &
cluster_pid=$!
build_started="$SECONDS"
if bash deploy/worker/base-current.sh ghcr.io/the-cloud-clockwork/agentihooks-worker:dev; then
  worker_base=published
  docker tag ghcr.io/the-cloud-clockwork/agentihooks-worker:dev local/worker-base:dev
else
  worker_base=rebuilt
  docker build -f docker/swarm-node/Dockerfile -t local/worker-base:dev .
fi
docker build -f deploy/worker/Dockerfile --build-arg WORKER_IMAGE=local/worker-base:dev --build-arg AGENTIHOOKS_REF="${AGENTIHOOKS_REF:-dev}" -t "$image" .
printf 'worker_hive_image_build_seconds=%s\nworker_base=%s\n' "$((SECONDS - build_started))" "$worker_base" | tee "$out/build.txt"
docker run --rm --tmpfs /home/worker:uid=10001,gid=10001 --entrypoint bash "$image" -c 'agentihooks init --profile engineer && grep -qx "<!-- profile: engineer -->" "$HOME/.claude/CLAUDE.md"' > "$out/init.txt"
wait "$cluster_pid"
kind load docker-image "$image" --name "$cluster"
docker run --rm --entrypoint bash "$image" -c 'claude --version && codex --version && herdr --version && git --version && bash /usr/local/bin/wt.sh root && node --version && npm --version && uv --version && gh --version && ruff --version && test "$(npm config get prefix)" = "$HOME/.local" && [[ ":$PATH:" == *":$HOME/.local/bin:"* ]] && agentihooks hive --help' > "$out/tools.txt"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=hive-join -addext subjectAltName=DNS:hive-join \
  -keyout "$out/key.pem" -out "$out/cert.pem" 2>/dev/null
kubectl create secret generic hive-tls --from-file=key.pem="$out/key.pem" --from-file=cert.pem="$out/cert.pem" >/dev/null
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hive-redis
spec:
  selector:
    matchLabels: {proof: redis}
  template:
    metadata:
      labels: {proof: redis}
    spec:
      containers:
        - name: redis
          image: mirror.gcr.io/library/redis:7-alpine
          readinessProbe:
            tcpSocket: {port: 6379}
            periodSeconds: 1
---
apiVersion: v1
kind: Service
metadata:
  name: hive-redis
spec:
  selector: {proof: redis}
  ports: [{port: 6379}]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hive-join
spec:
  selector:
    matchLabels: {proof: join}
  template:
    metadata:
      labels: {proof: join}
    spec:
      securityContext: {runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001}
      containers:
        - name: join
          image: $image
          imagePullPolicy: Never
          command: [agentihooks, hive, serve, --host, 0.0.0.0, --port, "8770", --tls-cert, /tls/cert.pem, --tls-key, /tls/key.pem]
          readinessProbe:
            tcpSocket: {port: 8770}
            periodSeconds: 1
          env:
            - {name: AGENTIHOOKS_SWARM_REDIS_URL, value: "redis://hive-redis:6379/0"}
          volumeMounts:
            - {name: tls, mountPath: /tls, readOnly: true}
      volumes:
        - name: tls
          secret: {secretName: hive-tls}
---
apiVersion: v1
kind: Service
metadata:
  name: hive-join
spec:
  selector: {proof: join}
  ports: [{port: 8770}]
EOF
kubectl rollout status deployment/hive-redis --timeout=120s
kubectl rollout status deployment/hive-join --timeout=120s
member_env="$(kubectl exec deployment/hive-join -- python -c '
import os
from scripts.hive import auth
from scripts.swarm.store import redis_client
print(auth.env_text("https://hive-join:8770", auth.mint(redis_client(), "worker", os.environ["AGENTIHOOKS_SWARM_REDIS_URL"])), end="")
')"
printf '%s' "$member_env" | kubectl create secret generic hive-member --from-file=credential=/dev/stdin >/dev/null
unset member_env
sha256sum -c deploy/helm/agentihooks-swarm-deps.sha256
helm template proof deploy/helm/agentihooks-swarm --show-only templates/worker-hive.yaml \
  --set workers.replicas=1 --set workers.maxAgents=1 --set workers.drainSeconds=120 --set workers.image.repository=local/agentihooks-hive-worker \
  --set workers.image.pullPolicy=Never --set workers.credentialSecret.name=hive-member --set workers.credentialSecret.key=credential \
  --set workers.caSecret.name=hive-tls --set workers.caSecret.key=cert.pem | kubectl apply -f -
deployment=proof-agentihooks-swarm-worker-hive
kubectl rollout status "deployment/$deployment" --timeout=120s
prove_worker() {
kubectl exec -i deployment/hive-join -- env HIVE="$1" python - <<'PY'
import json, os, time
from scripts.hive import registry
from scripts.hive.orders import CommandOrder, Orders
from scripts.swarm.store import redis_client

redis, hive = redis_client(), os.environ['HIVE']
record = None
for _ in range(60):
    record = registry.show(redis, hive)
    if record and registry.live(record, int(time.time() * 1000)):
        break
    time.sleep(1)
assert record and registry.live(record, int(time.time() * 1000)), 'pod did not register a live hive'
assert record['ui'] == 'no' and record['ephemeral'] == 'yes'
assert record['excluded'] == ['master'] and record['max_agents'] == 1 and 'roles' not in record
print('pod registered a live worker hive with its excluded list and capacity')
orders = Orders(redis)
order = CommandOrder('scratch-reap', 'reap', name='scratch-absent')
orders.command(hive, order)
ack = None
for _ in range(60):
    ack = orders.command(hive, order)
    if ack:
        break
    time.sleep(1)
assert ack and ack.status == 'done', f'scratch order not acknowledged: {ack}'
assert redis.xlen(orders.commands(hive)) == 0
print(json.dumps({'scratch_order': order.action, 'ack': ack.status, 'pending': 0}, sort_keys=True))
PY
}
member_id() {
  local member
  for _ in {1..60}; do
    member="$(kubectl exec "$1" -- python3 -c 'import hooks.config; from scripts.hive.scope import member; print(member())')"
    if [[ -n "$member" ]]; then
      printf '%s\n' "$member"
      return
    fi
    sleep 1
  done
  echo 'pod did not acquire hive membership' >&2
  return 1
}
pod="$(kubectl get pods -l app.kubernetes.io/component=worker-hive -o jsonpath='{.items[0].metadata.name}')"
member="$(member_id "$pod")"
prove_worker "$member" | tee "$out/orders.txt"
kubectl logs "$pod" > "$out/worker.log"
restarts="$(kubectl get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
[[ "$restarts" == 0 ]]
hold_agent() {
kubectl exec -i deployment/hive-join -- env HIVE="$member" STEP="$1" NAME="${2:-}" python - <<'PY'
import os
from scripts.swarm.store import PREFIX, AgentRecord, RedisStore, redis_client

store = RedisStore(redis_client())
if os.environ['STEP'] == 'start':
    store.redis.sadd(f'{PREFIX}:index', 'drain-proof')
    store.names.adopt('drain-proof', '999999', '', '')
    name = store.names.next('drain-proof', 'eng')
    store.start_execution('drain-proof', AgentRecord(name, 'eng', 't1', seat='eng-1@drain-proof', hive=os.environ['HIVE']))
    print(name)
elif os.environ['STEP'] == 'draining':
    from scripts.hive import registry
    assert registry.show(store.redis, os.environ['HIVE'])['draining'] == 'yes', 'the preStop did not mark the hive draining'
else:
    store.drop_agent('drain-proof', os.environ['NAME'])
PY
}
held="$(hold_agent start)"
kubectl patch deployment "$deployment" --type merge -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'
kubectl rollout restart "deployment/$deployment"
sleep 45
[[ -n "$(kubectl get pod "$pod" -o jsonpath='{.status.containerStatuses[0].state.running.startedAt}')" ]] || { echo "the hive drain preStop let $pod stop while an agent worked" >&2; exit 1; }
[[ "$(kubectl get pods -l app.kubernetes.io/component=worker-hive -o name)" == "pod/$pod" ]] || { echo "a replacement hive pod started while $pod drained" >&2; exit 1; }
hold_agent draining
echo "the hive drain preStop kept $pod running and draining for 45 seconds of a roll while its agent worked, with no replacement pod" | tee "$out/drain.txt"
[[ -n "$(kubectl get pod "$pod" -o jsonpath='{.status.containerStatuses[0].state.running.startedAt}')" ]] || { echo "the hive drain preStop let $pod stop before its agent finished" >&2; exit 1; }
hold_agent finish "$held"
kubectl wait --for=delete "pod/$pod" --timeout=25s
hook_failures="$(kubectl get events --field-selector "involvedObject.name=$pod,reason=FailedPreStopHook" -o name)"
if [[ -n "$hook_failures" ]]; then
  echo "the hive drain preStop failed on $pod" >&2
  exit 1
fi
echo "the hive drain preStop released $pod within 25 seconds of its agent finishing" | tee -a "$out/drain.txt"
kubectl rollout status "deployment/$deployment" --timeout=120s
replacement="$(kubectl get pods -l app.kubernetes.io/component=worker-hive -o jsonpath='{.items[0].metadata.name}')"
[[ "$replacement" != "$pod" ]]
replacement_member="$(member_id "$replacement")"
[[ "$replacement_member" == "$member" ]]
prove_worker "$replacement_member" | tee "$out/replacement.txt"
echo 'replacement pod reused the mounted member credential without minting again' | tee -a "$out/replacement.txt"
echo 'worker hive kind registration and scratch order acknowledgement: passed'
