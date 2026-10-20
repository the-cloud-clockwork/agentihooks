#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
revision="$(git rev-parse HEAD)"
output="${1:?supply a task evidence directory}"
proof="${2:-all}"
case "$proof" in
    all|startup|rebuild|profiles|compare) ;;
    *) echo "unknown worker proof: $proof" >&2; exit 2 ;;
esac
if [[ "$proof" == compare ]]; then
    python3 - "$output" "${3:?supply the tested revision}" <<'PY'
import json, sys
from pathlib import Path
output=Path(sys.argv[1])
revision=sys.argv[2]
fixtures={}
for proof in ('startup','rebuild','profiles'):
    result=json.loads((output/proof/'mode-result.json').read_text())
    fixtures[proof]=result.pop('fixture')
    assert result=={'proof':proof,'tested_commit':revision,'status':'passed'},proof
reports={name:json.loads((output/proof/f'{name}.json').read_text()) for proof,name in (
    ('startup','first'),('startup','second'),('startup','rollback'),('rebuild','rebuild'),('profiles','profiles'))}
first=reports['first']
shared={'package':'SV2-IMG-01','fixture':fixtures['startup'],'leg_fixtures':fixtures,'tested_commit':revision,'mocked':False,'startup_network':'none','lock_sha256':first['manifest']['declared']['lock_sha256']}
(output/'c-result.json').write_text(json.dumps(shared|{'uncached_rebuild_inventory_equal':reports['rebuild']==first,'retained_image_started_after_rejected_builds':reports['rollback']==first,'worker_image_build_validation_failures':first['worker_image_build_validation_failures']},indent=2)+'\n')
assert all(report==first for report in reports.values()), 'independent worker inventories differ'
assert first['manifest']['source_revision']==revision
assert first['worker_image_build_validation_failures']==0
profiles=json.loads((output/'profiles/SV2-IMG-02/result.json').read_text())
assert profiles['status']=='passed' and profiles['tested_commit']==revision
for case in ('a','b'):
    (output/f'{case}-result.json').write_bytes((output/'startup'/f'{case}-result.json').read_bytes())
(output/'result.json').write_text(json.dumps(shared|{'cases':['A','B','C'],'status':'passed','production_rollout':'not exercised'},indent=2)+'\n')
print('SV2-IMG-01 passed: parallel startup, rejected builds, uncached rebuild on its own daemon, retained image after rejected builds and equal inventories')
PY
    exit 0
fi
build_args=()
if [[ "$proof" == rebuild ]]; then build_args=(--no-cache-filter worker); fi
mkdir -p "$output"
docker info --format '{{.DriverStatus}}' > "$output/docker-driver.txt"
if [[ "$(< "$output/docker-driver.txt")" == *io.containerd.snapshotter.v1* ]]; then
    build_args+=(--provenance=false --output "type=image,compression=uncompressed")
    if [[ -n "${WORKER_LAYERS:-}" ]]; then build_args+=(--cache-from "type=local,src=$WORKER_LAYERS"); fi
fi
fixture="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
image="agentihooks-worker-proof:$fixture"
rebuild="agentihooks-worker-rebuild:$fixture"
rejected="agentihooks-worker-rejected:$fixture"
context="$output/context-$fixture"
mkdir "$context"
git -C "$repo" archive HEAD | tar -x -C "$context"
cleanup() {
    if [[ -n "${scanner_pid:-}" ]]; then wait "$scanner_pid" || true; fi
    if [[ -n "${scan_pid:-}" ]]; then wait "$scan_pid" || true; fi
    if [[ -n "${profile_pid:-}" ]]; then wait "$profile_pid" || true; fi
    docker rm -f "worker-profile-positive-$fixture" "worker-profile-positive-second-$fixture" >/dev/null 2>&1 || true
    docker image rm "$image" "$rebuild" "$rejected" >/dev/null 2>&1 || true
    python3 - "$context" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1])
PY
}
trap cleanup EXIT
trap 'echo "worker proof failed at line $LINENO: $BASH_COMMAND (exit $?)" >&2' ERR

bash "$context/docker/swarm-node/scan.sh" --pull-scanner > "$output/scanner-pull.log" 2>&1 &
scanner_pid=$!
docker build "${build_args[@]}" --platform linux/amd64 ${WORKER_BASE_CONTEXT:+--build-context "$WORKER_BASE_CONTEXT"} --build-arg SOURCE_REVISION="$revision" \
    -f "$context/docker/swarm-node/Dockerfile" -t "$image" "$context" > "$output/build.log" 2>&1
wait "$scanner_pid"
if [[ "$proof" == rebuild ]]; then
    bash "$context/docker/swarm-node/scan.sh" "$image" "$output/credential-scan"
    docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
        --tmpfs /tmp "$image" > "$output/rebuild.json"
    python3 - "$output" "$revision" "$proof" "$fixture" <<'PYMODE'
import json, sys
from pathlib import Path
output=Path(sys.argv[1])
report=json.loads((output/'rebuild.json').read_text())
assert report['manifest']['source_revision']==sys.argv[2]
assert report['worker_image_build_validation_failures']==0
(output/'mode-result.json').write_text(json.dumps({'proof':sys.argv[3],'tested_commit':sys.argv[2],'status':'passed','fixture':sys.argv[4]})+'\n')
PYMODE
    exit 0
fi
if [[ "$proof" != profiles ]]; then
    bash "$context/docker/swarm-node/scan.sh" "$image" "$output/credential-scan" &
    scan_pid=$!
    for attempt in first second; do
        docker run --rm --network none --read-only \
            --tmpfs /home/worker:uid=10001,gid=10001 --tmpfs /tmp \
            "$image" > "$output/$attempt.json"
    done
    docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
        "$image" python -c 'from pathlib import Path; import os; assert os.getuid()==10001; assert not os.access("/opt/agentihooks/templates", os.W_OK); assert not [p for p in Path("/opt/venv").rglob("*") if not p.is_symlink() and p.lstat().st_mode & 0o222]; import hooks, scripts.swarm_v2.runtime; print("private home and local runtime imports passed")' \
        > "$output/compatibility.log"
    docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 --tmpfs /tmp \
        -v "$context/tests/contracts/swarm_v2/fixtures/checkpoint/current.json:/checkpoint.json:ro" \
        "$image" python -c 'import json; from pathlib import Path; from scripts.swarm_v2.contracts import load; load()["validators"]["checkpoint"].validate(json.loads(Path("/checkpoint.json").read_text())); print("installed checkpoint contract accepted")' \
        > "$output/checkpoint-contracts.log"

fi

if [[ "$proof" != startup ]]; then
    probe="$context/tests/swarm_node"
    fixtures="$context/docker/swarm-node/fixtures/profiles"
    chmod -R a+rX "$probe" "$fixtures"
    (
        probes=()
        names=()
        for case in positive positive-second rejection recovery rollback noexec; do
            volume=/home/worker:uid=10001,gid=10001,exec
            if [[ "$case" == noexec ]]; then
                volume=/home/worker:uid=10001,gid=10001
            fi
            lifetime=(--rm)
            if [[ "$case" == positive* ]]; then lifetime=(--name "worker-profile-$case-$fixture"); fi
            (
                docker run "${lifetime[@]}" --network none --read-only --tmpfs "$volume" --tmpfs /tmp \
                -v "$fixtures:/fixtures:ro" -v "$probe:/opt/probe:ro" \
                "$image" bash /opt/probe/profile-probe.sh "${case%-second}" \
                > "$output/profile-$case.json" 2> "$output/profile-$case.log"
            if [[ "$case" == positive* ]]; then
                exited="$(docker inspect --format '{{.State.FinishedAt}}' "worker-profile-$case-$fixture")"
                docker rm "worker-profile-$case-$fixture" > /dev/null
                python3 - "$output/profile-$case.json" "$exited" <<'PYEXIT'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
report = json.loads(path.read_text())
whole, _, fraction = sys.argv[2].rstrip('Z').partition('.')
exited = datetime.strptime(whole, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp() + float('0.' + (fraction or '0'))
latency = exited - report['codex_probe']['registered_at']
assert 0 <= latency < 2, f"Codex registration to container exit took {latency:.3f} seconds"
report['codex_probe']['registration_to_container_exit_seconds'] = latency
path.write_text(json.dumps(report) + '\n')
print(f"Codex registration to container exit took {latency:.3f} seconds")
PYEXIT
            fi
            ) &
            probes+=("$!")
            names+=("$case")
        done
        status=0
        for index in "${!probes[@]}"; do
            wait "${probes[$index]}" || {
                status=$?
                echo "worker profile probe ${names[$index]} failed with exit $status" >&2
                tail -n 40 "$output/profile-${names[$index]}.log" "$output/profile-${names[$index]}.json" >&2
            }
        done
        if (( status )); then exit "$status"; fi
        python3 "$probe/profile_evidence.py" "$output" "$fixture" "$revision" "$fixtures" || {
            status=$?
            echo "worker profile evidence failed with exit $status" >&2
            exit "$status"
        }
    ) &
    profile_pid=$!
fi
if [[ "$proof" == profiles ]]; then
    bash "$context/docker/swarm-node/scan.sh" "$image" "$output/credential-scan"
    docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
        --tmpfs /tmp "$image" > "$output/profiles.json"
    wait "$profile_pid"
    python3 - "$output" "$revision" "$proof" "$fixture" <<'PYMODE'
import json, sys
from pathlib import Path
(Path(sys.argv[1])/'mode-result.json').write_text(json.dumps({'proof':sys.argv[3],'tested_commit':sys.argv[2],'status':'passed','fixture':sys.argv[4]})+'\n')
PYMODE
    exit 0
fi

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
    if docker build --platform linux/amd64 ${WORKER_BASE_CONTEXT:+--build-context "$WORKER_BASE_CONTEXT"} -f "$context/docker/swarm-node/Dockerfile" \
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
if [[ "$proof" == all ]]; then
    docker build --no-cache-filter worker --platform linux/amd64 ${WORKER_BASE_CONTEXT:+--build-context "$WORKER_BASE_CONTEXT"} --build-arg SOURCE_REVISION="$revision" \
        -f "$context/docker/swarm-node/Dockerfile" -t "$rebuild" "$context" > "$output/rebuild.log" 2>&1
    bash "$context/docker/swarm-node/scan.sh" "$rebuild" "$output/credential-scan-rebuild"
    docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
        --tmpfs /tmp "$rebuild" > "$output/rebuild.json"
fi
docker run --rm --network none --read-only --tmpfs /home/worker:uid=10001,gid=10001 \
    --tmpfs /tmp "$image" > "$output/rollback.json"
wait "$scan_pid"
if [[ -n "${profile_pid:-}" ]]; then wait "$profile_pid"; fi
python3 - "$output" "$fixture" "$revision" "$proof" <<'PY'
import json, sys
from pathlib import Path
output=Path(sys.argv[1])
reports={name:json.loads((output/f'{name}.json').read_text()) for name in (('first','second','rebuild','rollback') if sys.argv[4]=='all' else ('first','second','rollback'))}
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
    if case=='c' and sys.argv[4]!='all':
        continue
    (output/f'{case}-result.json').write_text(json.dumps(shared|details,indent=2)+'\n')
(output/'mode-result.json').write_text(json.dumps({'proof':sys.argv[4],'tested_commit':sys.argv[3],'status':'passed','fixture':sys.argv[2]})+'\n')
if sys.argv[4]=='all':
    (output/'result.json').write_text(json.dumps(shared|{'cases':['A','B','C'],'status':'passed','production_rollout':'not exercised'},indent=2)+'\n')
print('SV2-IMG-01 passed: two offline startups, three rejected builds, uncached rebuild and retained image rollback' if sys.argv[4]=='all' else 'Worker startup and rejection proof passed')
PY
if [[ -n "${WORKER_KEEP_IMAGE:-}" ]]; then docker tag "$image" "$WORKER_KEEP_IMAGE"; fi
