#!/usr/bin/env bash
# Offline smoke of a CI lab worker image: the entrypoint self test, no sudo, bwrap, the baked lockfile digest, the venv imports
# and a trampoline of one file from the image's own working directory.
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
/opt/agentihooks-venv/bin/python -c 'import sys; from pathlib import Path; from scripts.ci_lab import run_mutation; names = run_mutation.trampoline(Path(sys.argv[1]), "scripts/sample.py"); assert len(names) > 1, names' "$tree"
CHECKS
