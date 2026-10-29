#!/usr/bin/env bash
set -euo pipefail

{ /opt/swarm-node/ssh/start.sh || echo "terminal endpoint did not start; task execution continues" >&2; } &
if [[ "${1:-}" == python && "${2:-}" == /opt/swarm-node/supervisor.py ]]; then
    exec "$@"
fi
exec /usr/bin/tini -- "$@"
