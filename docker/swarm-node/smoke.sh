#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
revision="$(git rev-parse HEAD)"
output="${1:?supply a task evidence directory}"
fixture="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
image="agentihooks-worker-proof:$fixture"
rebuild="agentihooks-worker-rebuild:$fixture"
rejected="agentihooks-worker-rejected:$fixture"
mkdir -p "$output"
context="$output/context-$fixture"
mkdir "$context"
git -C "$repo" archive HEAD | tar -x -C "$context"
cleanup() {
    docker image rm "$image" "$rebuild" "$rejected" >/dev/null 2>&1 || true
    python3 - "$context" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1])
PY
}
trap cleanup EXIT

docker build --platform linux/amd64 --build-arg SOURCE_REVISION="$revision" \
    -f "$context/docker/swarm-node/Dockerfile" -t "$image" "$context" > "$output/build.log" 2>&1
for attempt in first second; do
    docker run --rm --network none --read-only \
        --tmpfs /home/worker:uid=10001,gid=10001 --tmpfs /tmp \
        "$image" > "$output/$attempt.json"
done
docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
    "$image" python -c 'from pathlib import Path; import os; assert os.getuid()==10001; assert not os.access("/opt/agentihooks/templates", os.W_OK); import hooks, scripts.swarm_v2.runtime; print("private home and local runtime imports passed")' \
    > "$output/compatibility.log"

for rejection in missing-checksum unsupported-architecture mismatched-binary; do
    python3 - "$context/docker/swarm-node/versions.lock" "$rejection" <<'PY'
import json, sys
from pathlib import Path
path=Path(sys.argv[1])
original=path.with_suffix('.original')
if not original.exists():
    original.write_bytes(path.read_bytes())
lock=json.loads(original.read_text())
if sys.argv[2]=='missing-checksum':
    del lock['tools']['herdr']['sha256']
elif sys.argv[2]=='unsupported-architecture':
    lock['architectures']=['arm64']
else:
    lock['tools']['herdr']['sha256']='0'*64
path.write_text(json.dumps(lock))
PY
    if docker build --platform linux/amd64 -f "$context/docker/swarm-node/Dockerfile" \
        -t "$rejected" "$context" > "$output/$rejection.log" 2>&1; then
        echo "rejection fixture unexpectedly built: $rejection" >&2
        exit 1
    fi
    if docker image inspect "$rejected" > /dev/null 2>&1; then
        echo 'rejection published an image' >&2
        exit 1
    fi
done
mv "$context/docker/swarm-node/versions.original" "$context/docker/swarm-node/versions.lock"
docker build --no-cache --platform linux/amd64 --build-arg SOURCE_REVISION="$revision" \
    -f "$context/docker/swarm-node/Dockerfile" -t "$rebuild" "$context" > "$output/rebuild.log" 2>&1
docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
    --tmpfs /tmp "$rebuild" > "$output/rebuild.json"
docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
    --tmpfs /tmp "$image" > "$output/rollback.json"
python3 - "$output" "$fixture" "$revision" <<'PY'
import json, sys
from pathlib import Path
output=Path(sys.argv[1])
reports={name:json.loads((output/f'{name}.json').read_text()) for name in ('first','second','rebuild','rollback')}
first=reports['first']
assert all(report==first for report in reports.values())
assert first['manifest']['source_revision']==sys.argv[3]
assert first['worker_image_build_validation_failures']==0
for name,expected in (('missing-checksum','invalid artifact lock: herdr'),('unsupported-architecture','unsupported architecture'),('mismatched-binary','artifact checksum mismatch: herdr')):
    assert expected in (output/f'{name}.log').read_text(),name
shared={'package':'SV2-IMG-01','fixture':sys.argv[2],'tested_commit':sys.argv[3],'mocked':False,'startup_network':'none','lock_sha256':first['manifest']['declared']['lock_sha256']}
for case,details in {
    'a':{'independent_startups':2,'worker_image_build_validation_failures':0,'manifest':first['manifest']},
    'b':{'rejected':['missing checksum','unsupported architecture','mismatched binary'],'published_rejected_image':False,'worker_image_build_validation_failures':3},
    'c':{'uncached_rebuild_inventory_equal':True,'retained_previous_image_started':True,'worker_image_build_validation_failures':0}
}.items():
    (output/f'{case}-result.json').write_text(json.dumps(shared|details,indent=2)+'\n')
(output/'result.json').write_text(json.dumps(shared|{'cases':['A','B','C'],'status':'passed','production_rollout':'not exercised'},indent=2)+'\n')
print('SV2-IMG-01 passed: two offline startups, three rejected builds, uncached rebuild and retained image rollback')
PY
