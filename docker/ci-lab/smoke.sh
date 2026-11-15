#!/usr/bin/env bash
# Offline smoke of a CI lab worker image: the entrypoint self test, no sudo, bwrap, the baked lockfile digest and the venv imports.
set -euo pipefail

image="${1:?usage: smoke.sh IMAGE}"

docker run --rm --network none "$image" --self-test

docker run --rm -i --network none --entrypoint bash "$image" -s <<'CHECKS'
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
grep -Eqx '[0-9a-f]{64}' /opt/agentihooks-venv/lockfile.sha256
/opt/agentihooks-venv/bin/python -c 'import scripts.ci_lab.contract, pytest, xdist, redis, nats, yaml, boto3'
CHECKS
