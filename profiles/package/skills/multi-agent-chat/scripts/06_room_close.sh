#!/usr/bin/env bash
# Leave a room (this agent only) or close it for everyone.
# Inputs: --room <target> --name <agent-name> --harness <h> (--leave | --close) [--reason <text>]
# Outputs: confirmation line.
# Idempotent: yes — leaving twice or closing twice is harmless.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

ROOM="latest"; NAME=""; HARNESS=""; MODE=""; REASON="operator ended the session"
while [ $# -gt 0 ]; do
  case "$1" in
    --room) ROOM="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --harness) HARNESS="$2"; shift 2 ;;
    --leave) MODE=leave; shift ;;
    --close) MODE=close; shift ;;
    --reason) REASON="$2"; shift 2 ;;
    *) mac_die "unknown argument '$1'" ;;
  esac
done
[ -n "$NAME" ] || mac_die "--name is required"
[ -n "$HARNESS" ] || mac_die "--harness is required"
[ -n "$MODE" ] || mac_die "pass --leave or --close"

mac_need flock util-linux
FILE="$(mac_resolve_room "$ROOM")"
ROOM_ID="$(mac_room_id "$FILE")"
SD="$(mac_state_dir "$ROOM_ID")"
SLUG="$(mac_slug "$NAME")"

if [ ! -f "$SD/closed" ]; then
  mac_append "$FILE" "$SD" "$MAC_DELIM
Agent Name: $NAME
Harness: $HARNESS
sent: $(mac_now)
to: all
msg-id: $(mac_uuid)
message: control: $MODE — $REASON
$MAC_DELIM

"
fi
rm -f "$SD/roster/$SLUG"
if [ "$MODE" = close ]; then
  : > "$SD/closed"
  printf 'closed room=%s — every participant'"'"'s waiter exits with code 3\n' "$ROOM_ID"
else
  printf 'left room=%s as %s — the room stays open for the others\n' "$ROOM_ID" "$NAME"
fi
