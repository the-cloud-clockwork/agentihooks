#!/usr/bin/env bash
# Append one record to a room under an exclusive lock.
# Inputs: --room <target> --name <agent-name> --harness <h> [--to <name|all>]
#         and one of --message <text> | --message-file <path> | - (stdin)
# Outputs: msg-id and new room size.
# Idempotent: no — each run appends one record.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

ROOM="latest"; NAME=""; HARNESS=""; TO="all"; BODY=""; SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --room) ROOM="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --harness) HARNESS="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    --message) BODY="$2"; SRC=arg; shift 2 ;;
    --message-file) BODY="$(cat "$2")"; SRC=file; shift 2 ;;
    -) BODY="$(cat)"; SRC=stdin; shift ;;
    *) mac_die "unknown argument '$1'" ;;
  esac
done
[ -n "$NAME" ] || mac_die "--name is required"
[ -n "$HARNESS" ] || mac_die "--harness is required"
[ -n "$SRC" ] || mac_die "provide --message, --message-file, or - for stdin"
[ -n "${BODY//[[:space:]]/}" ] || mac_die "message body is empty"

if printf '%s' "$BODY" | grep -qE 'sk-[A-Za-z0-9]{16}|ghp_[A-Za-z0-9]{20}|github_pat_[A-Za-z0-9_]{20}|AKIA[0-9A-Z]{12}|xox[baprs]-[A-Za-z0-9-]{10}|BEGIN [A-Z ]*PRIVATE KEY'; then
  mac_die "refusing to send: the body looks like it carries a credential. The room is plaintext and is read by several vendors' models — pass a variable name, never a value."
fi

mac_need flock util-linux
FILE="$(mac_resolve_room "$ROOM")"
ROOM_ID="$(mac_room_id "$FILE")"
SD="$(mac_state_dir "$ROOM_ID")"
SLUG="$(mac_slug "$NAME")"
[ -f "$SD/closed" ] && mac_die "room $ROOM_ID is closed — open a new one"
[ -f "$SD/roster/$SLUG" ] || mac_die "'$NAME' is not in room $ROOM_ID — join it first"

SAFE="$(printf '%s' "$BODY" | sed 's/^-\{20,\}$/ &/')"
MID="$(mac_uuid)"
mac_append "$FILE" "$SD" "$MAC_DELIM
Agent Name: $NAME
Harness: $HARNESS
sent: $(mac_now)
to: $TO
msg-id: $MID
message: $SAFE
$MAC_DELIM

"
mac_heartbeat "$SD" "$SLUG"
printf 'sent msg-id=%s to=%s room=%s size=%s\n' "$MID" "$TO" "$ROOM_ID" "$(stat -c%s "$FILE")"
