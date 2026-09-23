#!/usr/bin/env bash
# Report room state: every room, or one room's roster, liveness, size and cursors.
# Inputs: [--room <target>] [--name <agent-name>] [--stale S]
# Outputs: human-readable status on stdout.
# Idempotent: yes — read-only.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

ROOM=""; NAME=""; STALE=600
while [ $# -gt 0 ]; do
  case "$1" in
    --room) ROOM="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --stale) STALE="$2"; shift 2 ;;
    *) mac_die "unknown argument '$1'" ;;
  esac
done
mac_init_root
NOW="$(date +%s)"

if [ -z "$ROOM" ]; then
  printf '## Rooms in %s\n' "$MAC_ROOT"
  found=0
  for f in "$MAC_ROOT"/chat-*.txt; do
    [ -f "$f" ] || continue
    found=1
    id="$(mac_room_id "$f")"; sd="$(mac_state_dir "$id")"
    state=open; [ -f "$sd/closed" ] && state=closed
    n=0; [ -d "$sd/roster" ] && n="$(find "$sd/roster" -type f | wc -l)"
    printf -- '- %s  %s  %s bytes  %s participants  idle %ss\n' \
      "$id" "$state" "$(stat -c%s "$f")" "$n" "$(( NOW - $(stat -c%Y "$f") ))"
  done
  [ "$found" -eq 0 ] && printf -- '- none\n'
  exit 0
fi

FILE="$(mac_resolve_room "$ROOM")"
ROOM_ID="$(mac_room_id "$FILE")"
SD="$(mac_state_dir "$ROOM_ID")"
SIZE="$(stat -c%s "$FILE")"
STATE=open; [ -f "$SD/closed" ] && STATE=closed
printf 'room_id=%s\nroom_file=%s\nstate=%s\nsize=%s\nidle_seconds=%s\n\n' \
  "$ROOM_ID" "$FILE" "$STATE" "$SIZE" "$(( NOW - $(stat -c%Y "$FILE") ))"

printf '## Participants\n'
for r in "$SD"/roster/*; do
  [ -f "$r" ] || continue
  slug="$(basename "$r")"; age="$(( NOW - $(stat -c%Y "$r") ))"
  live=live; [ "$age" -gt "$STALE" ] && live="stale(${age}s)"
  cur=0; [ -f "$SD/cursor/$slug" ] && cur="$(cat "$SD/cursor/$slug")"
  printf -- '- %s (%s) %s unread=%s bytes\n' \
    "$(sed -n 's/^name=//p' "$r")" "$(sed -n 's/^harness=//p' "$r")" "$live" "$(( SIZE - cur ))"
done

if [ -n "$NAME" ]; then
  slug="$(mac_slug "$NAME")"
  if [ -f "$SD/roster/$slug" ]; then printf '\nyou=%s registered\n' "$NAME"
  else printf '\nyou=%s NOT registered — join the room\n' "$NAME"; fi
fi
