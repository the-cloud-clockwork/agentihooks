#!/usr/bin/env bash
set -euo pipefail

material="${SWARM_TERMINAL_MATERIAL:-/var/run/swarm/terminal}"
state="${SWARM_TERMINAL_STATE:-$HOME/.terminal}"
port="${SWARM_TERMINAL_PORT:-2222}"
listen="${SWARM_TERMINAL_LISTEN:-0.0.0.0}"
attempts="${SWARM_TERMINAL_ATTEMPTS:-/home/worker/attempts}"
launch="${SWARM_TERMINAL_LAUNCH:-/var/run/swarm/launch/launch.json}"
python="${SWARM_TERMINAL_PYTHON:-/opt/venv/bin/python}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for name in host_key user_ca.pub principals; do
    if [[ ! -s "$material/$name" ]]; then
        echo "terminal endpoint off: $material/$name is missing" >&2
        exit 0
    fi
done

umask 077
mkdir -p "$state"
install -m 0600 "$material/host_key" "$state/host_key"
sed -e "s|@PORT@|$port|" -e "s|@LISTEN@|$listen|" -e "s|@STATE@|$state|g" -e "s|@MATERIAL@|$material|g" \
    -e "s|@USER@|$(id -un)|" -e "s|@PYTHON@|$python|" -e "s|@ATTEMPTS@|$attempts|" -e "s|@LAUNCH@|$launch|" \
    "$here/sshd_config" > "$state/sshd_config"
exec /usr/sbin/sshd -f "$state/sshd_config" -E "$state/sshd.log"
