#!/usr/bin/env bash
set -euo pipefail
exec python -m scripts.swarm_v2.worker.git_auth "$@"
