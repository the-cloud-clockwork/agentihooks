#!/usr/bin/env bash
set -euo pipefail
# Sync brain-outbox marker files to Anton vault, then quick-refresh brain-feed.
# Cron: */5 * * * *

OUTBOX="${BRAIN_WRITER_OUTBOX:-$HOME/.agentihooks/brain-outbox}"
VAULT_SSH="${BRAIN_WRITER_VAULT_SSH:-root@10.10.30.130}"
VAULT_PATH="${BRAIN_WRITER_VAULT_PATH:-/mnt/user/appdata/obsidian/vault}"
SSH_KEY="${BRAIN_WRITER_SSH_KEY:-$HOME/.ssh/anton_id_ed25519}"
TODAY=$(date -u +%Y-%m-%d)
CLUSTER_DIR="$VAULT_PATH/clusters/$TODAY"

if [[ ! -d "$OUTBOX" ]] || [[ -z "$(ls -A "$OUTBOX" 2>/dev/null)" ]]; then
    exit 0
fi

SSH_CMD="ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=5 $VAULT_SSH"

# Ensure today's cluster dir exists
$SSH_CMD "mkdir -p '$CLUSTER_DIR' && chmod 777 '$CLUSTER_DIR'" 2>/dev/null || true

PROCESSED=0
for f in "$OUTBOX"/*.json; do
    [[ -f "$f" ]] || continue

    TYPE=$(python3 -c "import json; print(json.load(open('$f'))['type'])")
    CONTENT=$(python3 -c "import json; c=json.load(open('$f'))['content']; print(c[:500])")
    SID=$(python3 -c "import json; print(json.load(open('$f'))['session_id'][:8])")
    TS=$(python3 -c "import json; print(json.load(open('$f'))['ts'][:19])")

    ARC_FILE="$CLUSTER_DIR/${TODAY}-${SID}-writer.md"

    # Create arc stub if needed, then append marker
    $SSH_CMD "
        if [[ ! -f '$ARC_FILE' ]]; then
            cat > '$ARC_FILE' << 'FRONTMATTER'
---
cluster_id: ${TODAY}-${SID}-writer
title: Session markers — ${SID}
region: left-hemisphere
status: active
heat: 5
source_sessions:
  - ${SID}
created: ${TODAY}
---

# Session Markers
FRONTMATTER
            chmod 666 '$ARC_FILE'
        fi
        printf '\n<!-- @${TYPE} -->\n${CONTENT}\n<!-- @/${TYPE} -->\n' >> '$ARC_FILE'
    " 2>/dev/null

    rm "$f"
    PROCESSED=$((PROCESSED + 1))
done

if [[ "$PROCESSED" -gt 0 ]]; then
    # Quick-refresh brain-feed (scan arcs + write feeds only, no heat recompute)
    $SSH_CMD "python3 '$VAULT_PATH/brain-tools/brain_keeper.py' \
        --vault '$VAULT_PATH' --brain-feed '$VAULT_PATH/brain-feed' --quick-refresh" 2>/dev/null || true

    # Sync brain-feed back to local
    rsync -az -e "ssh -i $SSH_KEY" "$VAULT_SSH:$VAULT_PATH/brain-feed/" "$HOME/.agentihooks/brain-feed/" 2>/dev/null || true

    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) synced $PROCESSED markers" >> "$HOME/.agentihooks/logs/brain-outbox-sync.log"
fi
