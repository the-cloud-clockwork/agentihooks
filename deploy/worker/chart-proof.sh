#!/usr/bin/env bash
set -euo pipefail

chart=deploy/helm/agentihooks-swarm
export HTTPS_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9
out="$HOME/scratchpad/worker-hive-chart"
mkdir -p "$out"
sha256sum -c deploy/helm/agentihooks-swarm-deps.sha256
helm lint "$chart"
helm package "$chart" --version 0.0.1-plant --destination "$out" && tar tzf "$out/agentihooks-swarm-0.0.1-plant.tgz" | grep nats/Chart.yaml
helm template proof "$chart" --set workers.replicas=1 --set workers.maxAgents=3 \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential > "$out/rendered.yaml"
python3 - "$out/rendered.yaml" <<'PY'
import sys, yaml

objects = list(yaml.safe_load_all(open(sys.argv[1])))
workers = [o for o in objects if o and o.get('kind') == 'Deployment' and o['metadata']['name'].endswith('-worker-hive')]
assert len(workers) == 1, 'the worker hive Deployment is missing'
assert workers[0]['spec']['replicas'] == 1
pod = workers[0]['spec']['template']['spec']
assert pod['automountServiceAccountToken'] is False
container = pod['containers'][0]
env = {v['name']: v for v in container['env']}
assert env['SWARM_HIVE_ID']['valueFrom']['fieldRef']['fieldPath'] == 'metadata.name'
assert env['HIVE_MAX_AGENTS']['value'] == '3'
assert env['HIVE_EXCLUDED']['value'] == 'master'
assert container['image'] == 'ghcr.io/the-cloud-clockwork/agentihooks-hive-worker:dev'
assert pod['volumes'][0]['secret']['secretName'] == 'member'
assert pod['volumes'][0]['secret']['items'] == [{'key': 'credential', 'path': 'credential'}]
assert container['volumeMounts'][0]['readOnly'] is True
assert container['resources']['requests']['memory'] == '2100Mi', container['resources']
assert container['resources']['limits']['memory'] == '6144Mi', container['resources']
pinned = [key for key in ('nodeSelector', 'affinity', 'nodeName', 'hostNetwork') if key in pod]
assert not pinned, f'the worker hive pod is pinned by {pinned}'
print('worker replicas, identity, capacity, memory per agent, no node pin and mounted credentials: passed')
PY
if helm template proof "$chart" --set workers.replicas=1 > "$out/missing.out" 2> "$out/missing.err"; then
  echo 'workers accepted without a credentialSecret' >&2
  exit 1
fi
grep -q 'workers need a credentialSecret' "$out/missing.err"
if helm template proof "$chart" --set workers.replicas=2 \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential > "$out/shared.out" 2> "$out/shared.err"; then
  echo 'one member credential accepted for multiple replicas' >&2
  exit 1
fi
grep -q 'one hive credential is one hive' "$out/shared.err"
if helm template proof "$chart" --set workers.replicas=1 --set workers.memoryPerAgentMi.limit=100 \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential > "$out/memory.out" 2> "$out/memory.err"; then
  echo 'a per agent memory limit below its request was accepted' >&2
  exit 1
fi
grep -q 'workers.memoryPerAgentMi needs a positive request' "$out/memory.err"
if helm template proof "$chart" --set workers.replicas=1 --set workers.resources.limits.memory=4Gi \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential > "$out/fixed.out" 2> "$out/fixed.err"; then
  echo 'a fixed worker hive memory in workers.resources was accepted' >&2
  exit 1
fi
grep -q 'set worker hive memory with workers.memoryPerAgentMi' "$out/fixed.err"
echo 'missing and shared credentials, a per agent memory limit below its request and a fixed pod memory: refused'
helm template proof "$chart" --set workers.replicas=1 \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential \
  --set workers.ssh.enabled=true > "$out/ssh.yaml"
python3 - "$out/ssh.yaml" <<'PY'
import sys, yaml

objects = list(yaml.safe_load_all(open(sys.argv[1])))
worker = next(o for o in objects if o and o.get('kind') == 'Deployment' and o['metadata']['name'].endswith('-worker-hive'))
pod = worker['spec']['template']['spec']
container = pod['containers'][0]
env = {v['name']: v['value'] for v in container['env'] if 'value' in v}
assert env['HIVE_SSH_AUTHORIZED_KEYS_FILE'] == '/run/hive-join/ssh/authorized_keys'
assert env['HIVE_SSH_HOST_KEY_FILE'] == '/run/hive-join/ssh/host_key'
assert env['HIVE_SSH_PORT'] == '2222' and 'HIVE_PROFILE' not in env
assert container['ports'] == [{'name': 'ssh', 'containerPort': 2222}]
join = next(v for v in pod['volumes'] if v['name'] == 'join')
assert [i['path'] for i in join['secret']['items']] == ['credential', 'ssh/authorized_keys', 'ssh/host_key']
print('worker hive sshd keys, host key and port, with no profile flag: passed')
PY
if helm template proof "$chart" --set workers.replicas=1 \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential \
  --set workers.ssh.enabled=true --set workers.ssh.port=22 > "$out/port.out" 2> "$out/port.err"; then
  echo 'a privileged worker hive sshd port was accepted' >&2
  exit 1
fi
grep -q 'workers.ssh.port must be 1024 or higher' "$out/port.err"
echo 'privileged worker hive sshd port: refused'
