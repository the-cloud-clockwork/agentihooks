#!/usr/bin/env bash
set -euo pipefail
[[ -n "${AGENTIHOOKS_QUICK_SEAT:-}" || ( -n "${AGENTIHOOKS_SWARM:-}" && -n "${AGENTIHOOKS_AGENT_NAME:-}" ) ]] || exit 0
[[ "${AH_SESSION_ID:-}" =~ ^[A-Za-z0-9_-]+$ ]] || exit 0
[[ ! -f "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/first-read/$AH_SESSION_ID" ]] || exit 0
read -r shebang < "$(command -v agentihooks)"
exec "${shebang#\#!}" -P -m scripts.gates first-read
