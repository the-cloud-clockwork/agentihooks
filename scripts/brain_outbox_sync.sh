#!/usr/bin/env bash
set -euo pipefail
# Sync brain-outbox marker files to Anton vault, then quick-refresh brain-feed.
# Cron: */5 * * * *
#
# Hardening (2026-07-19):
#   - flock single-flight: overlapping cron fires raced the glob and died
#     mid-loop on rm under set -e.
#   - poison-pill quarantine: one malformed JSON used to wedge the drain
#     forever (parse failure -> set -e death -> file never removed -> every
#     future run dies on the same file, silently).
#   - true-ts date bucketing: arcs are dated by each marker's own `ts`, not
#     sync day. Stale backlogs no longer mass-file under "today" with fake
#     recency, and a re-emitted marker (hook re-scans the whole transcript
#     every Stop) lands in the SAME arc file, so the hash dedup actually
#     catches it across Stop events and days.
#   - timeout on the SSH write: ConnectTimeout only bounds the handshake; a
#     hung remote shell held the flock indefinitely.

OUTBOX="${BRAIN_WRITER_OUTBOX:-${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/brain-outbox}"
VAULT_SSH="${BRAIN_WRITER_VAULT_SSH:-root@10.10.30.130}"
VAULT_PATH="${BRAIN_WRITER_VAULT_PATH:-/mnt/user/appdata/obsidian/vault}"
SSH_KEY="${BRAIN_WRITER_SSH_KEY:-$HOME/.ssh/anton_id_ed25519}"
QUARANTINE="${OUTBOX%/}-quarantine"

if [[ ! -d "$OUTBOX" ]] || [[ -z "$(ls -A "$OUTBOX" 2>/dev/null)" ]]; then
    exit 0
fi

# Single-flight: a slow run (large backlog, per-file SSH) must not overlap the
# next 5-min cron fire — two instances racing the same glob leads to mid-loop
# rm failures and (with set -e) a dead half-processed run.
LOCK_FILE="${OUTBOX%/}.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

SSH_CMD="ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=5 $VAULT_SSH"

# Ensure logs dir exists
mkdir -p "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/logs"

PROCESSED=0
QUARANTINED=0
for f in "$OUTBOX"/*.json; do
    [[ -f "$f" ]] || continue

    # Single parse: type, sid8, true date (from ts), content[:500].
    # Tab-separated first line + content on following lines (content may
    # contain anything except we read it separately below to stay safe).
    if ! META=$(python3 -c "
import json, sys
d = json.load(open('$f'))
date = d['ts'][:10]
print(d['type'], d['session_id'][:8], date)
" 2>/dev/null); then
        mkdir -p "$QUARANTINE"
        mv "$f" "$QUARANTINE/" 2>/dev/null || true
        echo "WARN: quarantined malformed marker file $(basename "$f")" >&2
        QUARANTINED=$((QUARANTINED + 1))
        continue
    fi
    read -r TYPE SID MDATE <<< "$META"

    if ! CONTENT=$(python3 -c "
import json
d = json.load(open('$f'))
c = d['content'][:500].replace('\\\\', '\\\\\\\\').replace(\"'\", \"'\\\\''\")
print(c)
" 2>/dev/null); then
        mkdir -p "$QUARANTINE"
        mv "$f" "$QUARANTINE/" 2>/dev/null || true
        echo "WARN: quarantined malformed marker file $(basename "$f")" >&2
        QUARANTINED=$((QUARANTINED + 1))
        continue
    fi

    CLUSTER_DIR="$VAULT_PATH/clusters/$MDATE"
    ARC_FILE="$CLUSTER_DIR/${MDATE}-${SID}-writer.md"

    # Build the marker block locally, base64 encode, decode on remote
    MARKER_BLOCK=$(printf '\n<!-- @%s -->\n%s\n<!-- @/%s -->\n' "$TYPE" "$CONTENT" "$TYPE" | base64 | tr -d '\n')

    STUB_CONTENT=$(cat <<STUBEOF
---
cluster_id: ${MDATE}-${SID}-writer
title: Session markers — ${SID}
region: left-hemisphere
status: active
heat: 5
source_sessions:
  - ${SID}
created: ${MDATE}
---

# Session Markers
STUBEOF
)
    STUB_B64=$(echo "$STUB_CONTENT" | base64 | tr -d '\n')

    # Dedup: compute short hash of content, skip if already in arc
    CONTENT_HASH=$(echo "$CONTENT" | shasum -a 256 | cut -c1-8)

    if ! timeout 30 $SSH_CMD "
        mkdir -p '$CLUSTER_DIR' && chmod 777 '$CLUSTER_DIR' 2>/dev/null
        if [[ ! -f '$ARC_FILE' ]]; then
            echo '$STUB_B64' | base64 -d > '$ARC_FILE'
            chmod 666 '$ARC_FILE'
        fi
        if ! grep -qF '$CONTENT_HASH' '$ARC_FILE' 2>/dev/null; then
            echo '$MARKER_BLOCK' | base64 -d >> '$ARC_FILE'
            echo '<!-- hash:$CONTENT_HASH -->' >> '$ARC_FILE'
        fi
    " 2>/dev/null; then
        echo "WARN: failed to write $f (will retry next run)" >&2
        continue
    fi

    rm -f "$f"
    PROCESSED=$((PROCESSED + 1))
done

if [[ "$PROCESSED" -gt 0 ]] || [[ "$QUARANTINED" -gt 0 ]]; then
    # Sync brain-feed back to local (bounded — a hung transfer holds the flock)
    timeout 60 rsync -az -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=5" "$VAULT_SSH:$VAULT_PATH/brain-feed/" "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/brain-feed/" 2>/dev/null || true

    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) synced $PROCESSED markers, quarantined $QUARANTINED" >> "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/logs/brain-outbox-sync.log"
fi
