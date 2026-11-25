#!/usr/bin/env bash
# Offline smoke of a CI lab worker image: the entrypoint self test, no sudo, bwrap, the baked lockfile digest, the venv imports
# and a trampoline of one file from the image's own working directory, then a start of the worker as the lab chart runs it,
# on a full work root that belongs to another user, up to its first lease call.
set -euo pipefail

image="${1:?usage: smoke.sh IMAGE LOCK_DIGEST}"
digest="${2:?usage: smoke.sh IMAGE LOCK_DIGEST}"

docker run --rm --network none "$image" --self-test

docker run --rm -i --network none -e LOCK_DIGEST="$digest" --entrypoint bash "$image" -s <<'CHECKS'
set -euo pipefail
if command -v sudo; then
  echo "sudo is still installed" >&2
  exit 1
fi
if [[ -e /etc/sudoers || -e /etc/sudoers.d ]]; then
  echo "a sudoers file is still present" >&2
  exit 1
fi
bwrap --version
if [[ "$(cat /opt/agentihooks-venv/lockfile.sha256)" != "$LOCK_DIGEST" ]]; then
  echo "the baked lockfile digest is not the digest of the lock this image was built from" >&2
  exit 1
fi
/opt/agentihooks-venv/bin/python -c 'import scripts.ci_lab.contract, scripts.ci_lab.executors, scripts.ci_lab.run_tests, scripts.ci_lab.pricing, pytest, xdist, redis, nats, yaml, boto3'
tree="$(mktemp -d)"
mkdir "$tree/scripts"
printf 'def fee(amount, rate):\n    return amount * rate + 1\n' > "$tree/scripts/sample.py"
/opt/agentihooks-venv/bin/python -c 'import sys; from pathlib import Path; from scripts.ci_lab import run_mutation; names = run_mutation.trampoline(Path(sys.argv[1]), "scripts/sample.py", {2}); assert len(names) > 1, names' "$tree"
CHECKS

docker run --rm -i --network none --user 1001:1001 --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /work:mode=0777 --tmpfs /tmp:mode=1777 \
  -e CI_LAB_SCHEDULER_URL=http://127.0.0.1:9 -e CI_LAB_SLOTS=1 -e CI_LAB_GIT_MIRROR=/cache/git/agentihooks.git \
  -e UV_CACHE_DIR=/cache/uv -e CI_LAB_WORK_ROOT=/work -e CI_LAB_MANIFEST=/work/manifest.json \
  -e CI_LAB_FLAKES=/work/flakes.jsonl -e CI_LAB_MUTATION_CACHE=/work/mutation-cache -e CI_LAB_KILLERS=/work/killers \
  -e CI_LAB_MUTATION_BUDGET=1800 -e CI_LAB_MUTATION_CAP=1 --entrypoint bash "$image" -s <<'START'
set -euo pipefail
root="$CI_LAB_WORK_ROOT"
log="$(mktemp)"
mkdir -p "$root/trees/head/sealed" "$root/locked"
touch "$root/trees/head/sealed/file" "$root/manifest.json"
chmod 555 "$root/trees/head/sealed" "$root/trees/head"
chmod 000 "$root/locked"
agentihooks ci-lab-worker 2> "$log" &
worker=$!
trap 'kill "$worker" 2>/dev/null || true' EXIT
for _ in $(seq 240); do
  if ! kill -0 "$worker" 2>/dev/null; then
    cat "$log" >&2
    echo "the worker exited before its first lease call on a work root that belongs to another user" >&2
    exit 1
  fi
  if grep -q "lease call failed" "$log"; then
    break
  fi
  sleep 0.5
done
left="$(ls -A "$root" | tr '\n' ' ')"
if [[ "$left" != "runs venvs " ]]; then
  cat "$log" >&2
  echo "the worker made no lease call or left its work root holding: $left" >&2
  exit 1
fi
kill -TERM "$worker"
wait "$worker"
START
