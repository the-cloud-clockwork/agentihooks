#!/usr/bin/env bash
set -euo pipefail

chart=deploy/helm/agentihooks-swarm
out="$HOME/scratchpad/worker-hive-chart"
mkdir -p "$out"
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
assert env['HIVE_ROLES']['value'] == 'eng,ci,plan'
assert container['image'] == 'ghcr.io/the-cloud-clockwork/agentihooks-hive-worker:dev'
assert pod['volumes'][0]['secret']['secretName'] == 'member'
assert pod['volumes'][0]['secret']['items'] == [{'key': 'credential', 'path': 'credential'}]
assert container['volumeMounts'][0]['readOnly'] is True
print('worker replicas, identity, capacity and mounted credentials: passed')
PY
if helm template proof "$chart" --set workers.replicas=1 > "$out/missing.out" 2> "$out/missing.err"; then
  echo 'workers accepted without join material' >&2
  exit 1
fi
grep -q 'workers need a credentialSecret or joinCodeSecret' "$out/missing.err"
if helm template proof "$chart" --set workers.replicas=2 \
  --set workers.credentialSecret.name=member --set workers.credentialSecret.key=credential > "$out/shared.out" 2> "$out/shared.err"; then
  echo 'one member credential accepted for multiple replicas' >&2
  exit 1
fi
grep -q 'one hive credential is one hive' "$out/shared.err"
if helm template proof "$chart" --set workers.replicas=2 --set workers.joinUrl=https://join.example.test \
  --set workers.joinCodeSecret.name=code --set workers.joinCodeSecret.key=code > "$out/reused.out" 2> "$out/reused.err"; then
  echo 'one time code accepted for multiple replicas' >&2
  exit 1
fi
grep -q 'one time join codes require workers.replicas=1' "$out/reused.err"
helm template proof "$chart" --set workers.replicas=1 --set workers.joinUrl=https://join.example.test \
  --set workers.joinCodeSecret.name=code --set workers.joinCodeSecret.key=code > "$out/joined.yaml"
python3 - "$out/joined.yaml" <<'PY'
import sys, yaml

objects = list(yaml.safe_load_all(open(sys.argv[1])))
worker = next(o for o in objects if o and o.get('kind') == 'Deployment' and o['metadata']['name'].endswith('-worker-hive'))
assert worker['spec']['strategy']['type'] == 'Recreate'
home = next(v for v in worker['spec']['template']['spec']['volumes'] if v['name'] == 'home')
assert 'persistentVolumeClaim' in home, 'joined membership must survive pod replacement'
assert any(o and o.get('kind') == 'PersistentVolumeClaim' and o['metadata']['name'] == home['persistentVolumeClaim']['claimName'] for o in objects)
print('one time membership survives pod replacement: passed')
PY
echo 'missing, shared and reused join material: refused'
