#!/usr/bin/env bash
set -euo pipefail

# ruleid: periodic-shell-sleep-loop
while true; do
    agentihooks swarm tick
    sleep 60
done

# ruleid: machine-cron-entry
echo "* * * * * agentihooks swarm tick" | crontab -

# ruleid: periodic-shell-sleep-loop
while true; do
    if curl -fsS http://127.0.0.1:8765/health; then
        break
    fi
    sleep 1
done

# ruleid: periodic-shell-sleep-loop
while :; do
    agentihooks swarm tick
    sleep 60
done

# ruleid: periodic-shell-sleep-loop
until false; do
    agentihooks swarm tick
    sleep 60
done

# ruleid: periodic-shell-sleep-loop
while sleep 60; do
    agentihooks swarm tick
done

# ruleid: machine-cron-entry
echo "agentihooks swarm tick" | at 17:00

# ruleid: periodic-shell-sleep-loop
while [ 1 ]; do
    agentihooks swarm tick
    sleep 60
done

# ruleid: machine-cron-entry
echo "agentihooks swarm tick" | at 5pm

# ruleid: machine-cron-entry
echo "agentihooks gc" | at midnight
