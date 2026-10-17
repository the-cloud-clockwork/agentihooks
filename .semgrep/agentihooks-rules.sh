#!/usr/bin/env bash
set -euo pipefail

# ruleid: periodic-shell-sleep-loop
while true; do
    agentihooks swarm tick
    sleep 60
done

# ruleid: machine-cron-entry
echo "* * * * * agentihooks swarm tick" | crontab -

# ok: periodic-shell-sleep-loop
while true; do
    if curl -fsS http://127.0.0.1:8765/health; then
        break
    fi
    sleep 1
done
