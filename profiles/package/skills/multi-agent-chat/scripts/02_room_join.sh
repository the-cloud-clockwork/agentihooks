#!/usr/bin/env bash
# Join an existing room: resolve the target, register the caller, print the
# roster and the tail of the transcript, announce the join, park the cursor at EOF.
# Inputs: --room <room-id|uuid|path|latest> --name <agent-name> --harness <h> [--tail N]
# Outputs: KEY=VALUE room facts, roster, recent records.
# Idempotent: re-joining refreshes the roster entry and re-announces.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

ROOM="latest"; NAME=""; HARNESS=""; TAIL=12
while [ $# -gt 0 ]; do
  case "$1" in
    --room) ROOM="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --harness) HARNESS="$2"; shift 2 ;;
    --tail) TAIL="$2"; shift 2 ;;
    *) mac_die "unknown argument '$1'" ;;
  esac
done
[ -n "$NAME" ] || mac_die "--name is required"
[ -n "$HARNESS" ] || mac_die "--harness is required"

mac_need flock util-linux
mac_init_root
FILE="$(mac_resolve_room "$ROOM")"
ROOM_ID="$(mac_room_id "$FILE")"
SD="$(mac_state_dir "$ROOM_ID")"
mkdir -p "$SD/roster" "$SD/cursor"
SLUG="$(mac_slug "$NAME")"
[ -f "$SD/closed" ] && mac_die "room $ROOM_ID is closed"

mac_register "$SD" "$SLUG" "$NAME" "$HARNESS"
mac_append "$FILE" "$SD" "$MAC_DELIM
Agent Name: $NAME
Harness: $HARNESS
sent: $(mac_now)
to: all
msg-id: $(mac_uuid)
message: joined the room.
$MAC_DELIM

"
printf '%s' "$(stat -c%s "$FILE")" > "$SD/cursor/$SLUG"

printf 'room_id=%s\nroom_file=%s\nstate_dir=%s\nprotocol=%s\nagent_name=%s\nagent_slug=%s\n\n' \
  "$ROOM_ID" "$FILE" "$SD" "$MAC_ROOT/PROTOCOL.md" "$NAME" "$SLUG"

printf '## Roster\n'
for r in "$SD"/roster/*; do
  [ -f "$r" ] || continue
  printf -- '- %s (%s) last-seen %ss ago\n' \
    "$(sed -n 's/^name=//p' "$r")" "$(sed -n 's/^harness=//p' "$r")" \
    "$(( $(date +%s) - $(stat -c%Y "$r") ))"
done

printf '\n## Last %s records\n' "$TAIL"
awk -v delim="$MAC_DELIM" -v keep="$TAIL" '
  /^-{20,}$/ { b++; next }
  { rec[b] = rec[b] $0 "\n" }
  END {
    start = b - keep * 2; if (start < 0) start = 0
    for (i = start; i <= b; i++)
      if (rec[i] ~ /[Aa]gent [Nn]ame:/) printf "%s\n%s%s\n\n", delim, rec[i], delim
  }' "$FILE"
