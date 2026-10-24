#!/usr/bin/env bash
set -euo pipefail

attempt="$HOME/attempts/$EXECUTION_ID"
mkdir -p "$attempt/homes/codex" "$attempt/tmp"
mkdir -m 0700 -p "$attempt/run"
python - "$attempt" "$HOME/launch.json" <<'EOF'
import json, os, sys
from pathlib import Path

attempt, launch = Path(sys.argv[1]), Path(sys.argv[2])
authority = {
    "execution_id": os.environ["EXECUTION_ID"],
    "generation": int(os.environ["GENERATION"]),
    "task_id": "kind-terminal",
    "seat_id": "eng-4",
    "swarm_id": os.environ["SWARM"],
    "grant_id": "lgr-" + "e" * 32,
}
(attempt / "execution.json").write_text(json.dumps({"attempt": authority["execution_id"], "homes": {"codex": "homes/codex"}}))
(attempt / "registration.json").write_text(json.dumps(authority))
launch.write_text(json.dumps({"schema_version": 1, "authority": authority, "harness": "codex", "agent": ["true"]}))
EOF
SWARM_TERMINAL_LAUNCH="$HOME/launch.json" /opt/swarm-node/ssh/start.sh &
herdr_env=(env -i "HOME=$attempt/homes/codex" "XDG_RUNTIME_DIR=$attempt/run" "TMPDIR=$attempt/tmp" PATH=/usr/local/bin:/usr/bin:/bin TERM=dumb USER=worker)
setsid "${herdr_env[@]}" herdr server > "$HOME/herdr-server.log" 2>&1 &
for _ in $(seq 100); do
  if "${herdr_env[@]}" herdr status server --json 2>/dev/null | grep -q '"running":true'; then
    printf 'terminal worker ready: herdr server running for %s\n' "$EXECUTION_ID"
    wait
    exit
  fi
  sleep 0.2
done
printf 'the terminal worker herdr server never ran: %s\n' "$(cat "$HOME/herdr-server.log")" >&2
exit 1
