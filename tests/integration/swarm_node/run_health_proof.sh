#!/usr/bin/env bash
set -euo pipefail

task_output="${1:?output directory required}"
task_python="${SWARM_PROOF_PYTHON:-python3}"
task_repo="$(git rev-parse --show-toplevel)"
task_revision="$(git rev-parse HEAD)"
task_suffix="$("$task_python" -c 'import uuid; print(uuid.uuid4().hex)')"
task_worker="agentihooks-worker:$task_suffix"
task_fixture="agentihooks-health-fixture:$task_suffix"
task_missing="agentihooks-health-missing-binary:$task_suffix"
mkdir -p "$task_output"
cleanup() {
    docker image rm "$task_missing" "$task_fixture" "$task_worker" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build --platform linux/amd64 --file "$task_repo/docker/swarm-node/Dockerfile" \
    --build-arg SOURCE_REVISION="$task_revision" --tag "$task_worker" "$task_repo" > "$task_output/worker-build.log" 2>&1
docker build --file "$task_repo/tests/integration/swarm_node/Dockerfile" \
    --build-arg WORKER_IMAGE="$task_worker" --tag "$task_fixture" "$task_repo" > "$task_output/fixture-build.log" 2>&1
printf 'FROM %s\nUSER 0:0\nRUN rm /usr/local/bin/codex\nUSER 10001:10001\n' "$task_fixture" \
    | docker build --tag "$task_missing" - > "$task_output/missing-binary-build.log" 2>&1
"$task_python" "$task_repo/tests/integration/swarm_node/health_proof.py" \
    --image "$task_fixture" --missing-binary-image "$task_missing" \
    --tested-commit "$task_revision" --output "$task_output/results" 2>&1 | tee "$task_output/proof.log"
