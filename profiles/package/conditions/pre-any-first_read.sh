#!/usr/bin/env bash
set -euo pipefail
[[ "${AH_SESSION_ID:-}" =~ ^[A-Za-z0-9_-]+$ ]] || exit 0
first_read_home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/first-read"
[[ -n "${AGENTIHOOKS_QUICK_SEAT:-}" || ( -n "${AGENTIHOOKS_SWARM:-}" && -n "${AGENTIHOOKS_AGENT_NAME:-}" ) || -f "$first_read_home/.members/$AH_SESSION_ID" ]] || exit 0
[[ ! -f "$first_read_home/$AH_SESSION_ID" ]] || exit 0
read -r shebang < "$(command -v agentihooks)"
exec "${shebang#\#!}" -P -m scripts.gates first-read
