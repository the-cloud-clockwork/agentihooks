#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
owned=label=com.docker.compose.project=agentihooks-swarm
if [[ -n "$(docker ps --all --quiet --filter "$owned")$(docker volume ls --quiet --filter "$owned")" ]]; then
  printf 'A stack named agentihooks-swarm already runs on this host; the smoke would replace and remove it.\n' >&2
  exit 1
fi
work="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
export AGENTIHOOKS_HOME="$work/agentihooks-home" NO_COLOR=1
trap 'docker compose --project-name agentihooks-swarm down --volumes >/dev/null 2>&1 || true' EXIT
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" ghcr.io/the-cloud-clockwork/agentihooks-swarm:release
else
  bash scripts/packaging/pull-runtime-image.sh ghcr.io/the-cloud-clockwork/agentihooks-swarm:release
fi
"${SWARM_INIT_PYTHON:-python3}" -m venv "$work/venv"
SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 "$work/venv/bin/pip" install --quiet .
cd "$work"
for run in first second; do
  "$work/venv/bin/agentihooks" swarm init smoke-swarm --operator-asked "run the swarm init smoke" | tee "$work/$run.log"
  grep -qx 'Swarm stack ready: redis, swarm and controller are healthy.' "$work/$run.log"
done
first="$(sed -n 's/^  1\. Open the ledger: //p' "$work/first.log")"
second="$(sed -n 's/^  1\. Open the ledger: //p' "$work/second.log")"
[[ -n "$first" && "$first" == "$second" ]]
python3 - "$first" <<'PY'
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    body = response.read()
assert response.status == 200 and b"<title>HOME</title>" in body
print(f"GET {sys.argv[1]}: 200, ledger home HTML ({len(body)} bytes)")
PY
printf 'Swarm init smoke passed from the installed package: two runs, one ledger address\n'
