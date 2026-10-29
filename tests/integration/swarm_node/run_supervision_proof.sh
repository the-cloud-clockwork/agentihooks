#!/usr/bin/env bash
set -euo pipefail

task_output="${1:?output directory required}"
task_prior="${2:?retained prior worker image required}"
task_python="${SWARM_PROOF_PYTHON:-$HOME/dev/tcc-ecosystem/.venv/bin/python}"
task_repo="$(git rev-parse --show-toplevel)"
task_revision="$(git rev-parse HEAD)"
task_worker="$("$task_python" -c 'import uuid; print("agentihooks-worker:" + uuid.uuid4().hex)')"
task_fixture="$("$task_python" -c 'import uuid; print("agentihooks-supervision-fixture:" + uuid.uuid4().hex)')"
task_compatibility="$("$task_python" -c 'import uuid; print("agentihooks-compatibility-fixture:" + uuid.uuid4().hex)')"
mkdir -p "$task_output"
"$task_python" -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(dict(zip(("worker","fixture","prior_fixture","tested_commit"),sys.argv[2:])),indent=2))' \
    "$task_output/images.json" "$task_worker" "$task_fixture" "$task_compatibility" "$task_revision"
docker build --platform linux/amd64 --file "$task_repo/docker/swarm-node/Dockerfile" \
    --build-arg SOURCE_REVISION="$task_revision" --tag "$task_worker" "$task_repo" > "$task_output/worker-build.log" 2>&1
printf 'WORKER BUILD COMPLETE\n'
docker build --file "$task_repo/tests/integration/swarm_node/Dockerfile" \
    --build-arg WORKER_IMAGE="$task_worker" --tag "$task_fixture" "$task_repo" > "$task_output/fixture-build.log" 2>&1
printf 'FIXTURE BUILD COMPLETE\n'
docker build --file "$task_repo/tests/integration/swarm_node/Dockerfile" \
    --build-arg WORKER_IMAGE="$task_prior" --tag "$task_compatibility" "$task_repo" > "$task_output/compatibility-build.log" 2>&1
printf 'PRIOR COMPATIBILITY BUILD COMPLETE\n'
"$task_python" "$task_repo/tests/integration/swarm_node/supervision_proof.py" \
    --image "$task_fixture" --prior-image "$task_compatibility" \
    --tested-commit "$task_revision" --output "$task_output/results" > "$task_output/proof.log" 2>&1
printf 'CONTAINER ACCEPTANCE COMPLETE\n'
