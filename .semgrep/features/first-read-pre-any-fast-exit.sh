#!/usr/bin/env bash
set -euo pipefail
# ruleid: first-read-pre-any-fast-exit
exec "$python" -P -m scripts.gates first-read

[[ -n "${AGENTIHOOKS_SWARM:-}" ]] || exit 0
# ok: first-read-pre-any-fast-exit
exec "$python" -P -m scripts.gates first-read
