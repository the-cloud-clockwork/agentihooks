#!/usr/bin/env bash
set -euo pipefail

/opt/swarm-node/ssh/start.sh || echo "terminal endpoint did not start; task execution continues" >&2
exec /usr/bin/tini -- "$@"
