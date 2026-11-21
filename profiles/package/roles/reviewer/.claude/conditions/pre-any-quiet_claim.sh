#!/usr/bin/env bash
set -euo pipefail
[[ -n "${AGENTIHOOKS_SWARM:-}" && -n "${AGENTIHOOKS_AGENT_NAME:-}" ]] || exit 0
[[ -e "$HOME/.agentihooks/swarm/$AGENTIHOOKS_SWARM/gates/quiet/$AGENTIHOOKS_AGENT_NAME" ]] || exit 0
read -r shebang < "$(command -v agentihooks)"
exec "${shebang#\#!}" -P -m scripts.gates quiet
