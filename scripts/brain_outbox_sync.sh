#!/usr/bin/env bash
set -euo pipefail
# Sync brain-outbox marker files to Anton vault, then quick-refresh brain-feed.
# Cron: */5 * * * *

OUTBOX="${BRAIN_WRITER_OUTBOX:-${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/brain-outbox}"
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

# Ensure logs dir exists
mkdir -p "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/logs"

PROCESSED=0
for f in "$OUTBOX"/*.json; do
    [[ -f "$f" ]] || continue

    # Extract fields safely via python
    read -r TYPE SID <<< "$(python3 -c "
import json, sys
d = json.load(open('$f'))
print(d['type'], d['session_id'][:8])
")"
    CONTENT=$(python3 -c "
import json, sys
d = json.load(open('$f'))
# Escape for safe shell embedding
c = d['content'][:500].replace('\\\\', '\\\\\\\\').replace(\"'\", \"'\\\\''\")
print(c)
")

    ARC_FILE="$CLUSTER_DIR/${TODAY}-${SID}-writer.md"

    # Build the marker block locally, base64 encode, decode on remote
    MARKER_BLOCK=$(printf '\n<!-- @%s -->\n%s\n<!-- @/%s -->\n' "$TYPE" "$CONTENT" "$TYPE" | base64 -w0)

    STUB_CONTENT=$(cat <<STUBEOF
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
STUBEOF
)
    STUB_B64=$(echo "$STUB_CONTENT" | base64 -w0)

    # Dedup: compute short hash of content, skip if already in arc
    CONTENT_HASH=$(echo "$CONTENT" | md5sum | cut -c1-8)

    $SSH_CMD "
        if [[ ! -f '$ARC_FILE' ]]; then
            echo '$STUB_B64' | base64 -d > '$ARC_FILE'
            chmod 666 '$ARC_FILE'
        fi
        if ! grep -qF '$CONTENT_HASH' '$ARC_FILE' 2>/dev/null; then
            echo '$MARKER_BLOCK' | base64 -d >> '$ARC_FILE'
            echo '<!-- hash:$CONTENT_HASH -->' >> '$ARC_FILE'
        fi
    " 2>/dev/null || { echo "WARN: failed to write $f" >&2; continue; }

    rm "$f"
    PROCESSED=$((PROCESSED + 1))
done

if [[ "$PROCESSED" -gt 0 ]]; then
    # Sync brain-feed back to local
    rsync -az -e "ssh -i $SSH_KEY" "$VAULT_SSH:$VAULT_PATH/brain-feed/" "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/brain-feed/" 2>/dev/null || true

    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) synced $PROCESSED markers" >> "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/logs/brain-outbox-sync.log"
fi
