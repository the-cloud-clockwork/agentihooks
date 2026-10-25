#!/usr/bin/env bash
set -euo pipefail

material="${SWARM_TERMINAL_MATERIAL:-/var/run/swarm/terminal}"
state="${SWARM_TERMINAL_STATE:-$HOME/.terminal}"
port="${SWARM_TERMINAL_PORT:-2222}"
listen="${SWARM_TERMINAL_LISTEN:-0.0.0.0}"
attempts="${SWARM_TERMINAL_ATTEMPTS:-/home/worker/attempts}"
launch="${SWARM_TERMINAL_LAUNCH:-/var/run/swarm/launch/launch.json}"
python="${SWARM_TERMINAL_PYTHON:-/opt/venv/bin/python}"
session_path="${SWARM_TERMINAL_PATH:-/usr/local/bin:/usr/bin:/bin}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

wait_seconds="${SWARM_TERMINAL_WAIT_SECONDS:-180}"

mounted() {
    local name
    for name in host_key user_ca.pub principals; do
        [[ -s "$material/$name" ]] || return 1
    done
}

waited=0
until mounted; do
    if (( waited >= wait_seconds )); then
        echo "terminal endpoint off: no terminal material in $material after ${wait_seconds}s" >&2
        exit 0
    fi
    sleep 1
    waited=$((waited + 1))
done

umask 077
mkdir -p "$state"
for name in host_key user_ca.pub principals; do
    install -m 0600 "$material/$name" "$state/$name"
done
sed -e "s|@PORT@|$port|" -e "s|@LISTEN@|$listen|" -e "s|@STATE@|$state|g" -e "s|@MATERIAL@|$material|g" \
    -e "s|@USER@|$(id -un)|" -e "s|@PYTHON@|$python|" -e "s|@ATTEMPTS@|$attempts|" -e "s|@LAUNCH@|$launch|" -e "s|@PATH@|$session_path|" \
    "$here/sshd_config" > "$state/sshd_config"
exec /usr/sbin/sshd -f "$state/sshd_config" -E "$state/sshd.log"
