#!/usr/bin/env bash
# Block until the room holds records addressed to this agent, print them, and
# advance this agent's byte cursor. Prints nothing and costs nothing while idle.
# Inputs: --room <target> --name <agent-name> [--timeout S] [--poll S] [--once]
# Outputs: the new records on stdout.
# Exit: 0 new records | 2 timed out, nothing for us | 3 room closed | 1 error.
# Idempotent: yes — records already past the cursor are never re-emitted.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

ROOM="latest"; NAME=""; TIMEOUT=900; POLL=3; ONCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --room) ROOM="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --poll) POLL="$2"; shift 2 ;;
    --once) ONCE=1; shift ;;
    *) mac_die "unknown argument '$1'" ;;
  esac
done
[ -n "$NAME" ] || mac_die "--name is required"

FILE="$(mac_resolve_room "$ROOM")"
ROOM_ID="$(mac_room_id "$FILE")"
SD="$(mac_state_dir "$ROOM_ID")"
SLUG="$(mac_slug "$NAME")"
mkdir -p "$SD/cursor"
CURF="$SD/cursor/$SLUG"
CUR=0; [ -f "$CURF" ] && CUR="$(cat "$CURF")"

HAVE_INOTIFY=0
command -v inotifywait >/dev/null 2>&1 && HAVE_INOTIFY=1
START="$(date +%s)"

while :; do
  mac_heartbeat "$SD" "$SLUG"
  SIZE="$(stat -c%s "$FILE")"
  if [ "$SIZE" -lt "$CUR" ]; then
    printf 'warning: %s shrank (%s < %s) — rewinding cursor to 0\n' "$ROOM_ID" "$SIZE" "$CUR" >&2
    CUR=0; printf '%s' "$CUR" > "$CURF"
  fi
  if [ "$SIZE" -gt "$CUR" ]; then
    CHUNK="$(tail -c "+$((CUR + 1))" "$FILE")"
    CUR="$SIZE"; printf '%s' "$CUR" > "$CURF"
    set +e
    OUT="$(printf '%s\n' "$CHUNK" | awk -v self="$NAME" -v delim="$MAC_DELIM" '
      function flush(  i) {
        if (n > 0 && has_name && name != self && (to == "" || to == "all" || to == self)) {
          printf "%s\n", delim
          for (i = 1; i <= n; i++) printf "%s\n", rec[i]
          printf "%s\n\n", delim
          kept++
        }
        n = 0; has_name = 0; name = ""; to = ""; inbody = 0
      }
      BEGIN { n = 0; kept = 0; has_name = 0; inbody = 0 }
      /^-{20,}$/ { flush(); next }
      {
        rec[++n] = $0
        if (!inbody) {
          if ($0 ~ /^[Aa]gent [Nn]ame:[ \t]*/) { name = $0; sub(/^[^:]*:[ \t]*/, "", name); has_name = 1 }
          else if ($0 ~ /^to:[ \t]*/) { to = $0; sub(/^to:[ \t]*/, "", to) }
          else if ($0 ~ /^message:/) { inbody = 1 }
        }
      }
      END { flush(); exit (kept > 0 ? 0 : 10) }')"
    RC=$?
    set -e
    if [ "$RC" -eq 0 ]; then
      printf '## new in %s at %s (as %s)\n\n%s\n' "$ROOM_ID" "$(mac_now)" "$NAME" "$OUT"
      if [ -f "$SD/closed" ]; then exit 3; fi
      exit 0
    fi
  fi
  [ -f "$SD/closed" ] && exit 3
  [ "$ONCE" -eq 1 ] && exit 2
  if [ "$(( $(date +%s) - START ))" -ge "$TIMEOUT" ]; then exit 2; fi
  if [ "$HAVE_INOTIFY" -eq 1 ]; then
    inotifywait -qq -t "$POLL" -e modify,close_write "$FILE" >/dev/null 2>&1 || true
  else
    sleep "$POLL"
  fi
done
