#!/usr/bin/env bash
# Shared primitives for the multi-agent-chat room scripts.
# Sourced, never executed. Inputs: MULTI_AGENT_CHAT_HOME (optional).

MAC_ROOT="${MULTI_AGENT_CHAT_HOME:-$HOME/.multi-agent-chat}"
MAC_DELIM="-------------------------------------------"

mac_die() { printf 'error: %s\n' "$*" >&2; exit 1; }

mac_need() {
  command -v "$1" >/dev/null 2>&1 || mac_die "missing system dependency '$1' — ask the operator to install it ($2)"
}

mac_init_root() {
  mkdir -p "$MAC_ROOT/.state"
  chmod 700 "$MAC_ROOT"
  [ -f "$MAC_ROOT/.gitignore" ] || printf '*\n' > "$MAC_ROOT/.gitignore"
}

mac_now() { date +%Y-%m-%dT%H:%M:%S%:z; }

mac_uuid() {
  if [ -r /proc/sys/kernel/random/uuid ]; then
    cut -c1-8 /proc/sys/kernel/random/uuid
  else
    od -An -tx1 -N4 /dev/urandom | tr -d ' \n'
  fi
}

mac_slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-64; }

mac_room_id() { basename "$1" .txt; }

mac_state_dir() { printf '%s/.state/%s' "$MAC_ROOT" "$1"; }

mac_resolve_room() {
  local t="${1:-latest}" m c
  [ -f "$t" ] && { printf '%s\n' "$t"; return 0; }
  [ -f "$MAC_ROOT/$t" ] && { printf '%s\n' "$MAC_ROOT/$t"; return 0; }
  [ -f "$MAC_ROOT/$t.txt" ] && { printf '%s\n' "$MAC_ROOT/$t.txt"; return 0; }
  if [ "$t" = latest ]; then
    m=$(ls -1t "$MAC_ROOT"/chat-*.txt 2>/dev/null | head -1)
    [ -n "$m" ] || mac_die "no rooms in $MAC_ROOT — open one first"
    printf '%s\n' "$m"; return 0
  fi
  m=$(ls -1t "$MAC_ROOT"/chat-*.txt 2>/dev/null | grep -F -- "$t" || true)
  c=$(printf '%s' "$m" | grep -c . || true)
  [ "$c" -eq 0 ] && mac_die "no room matching '$t' in $MAC_ROOT"
  [ "$c" -gt 1 ] && mac_die "'$t' matches $c rooms — pass the full room id"
  printf '%s\n' "$m"
}

mac_register() {
  local sd="$1" slug="$2" name="$3" harness="$4"
  mkdir -p "$sd/roster" "$sd/cursor"
  printf 'name=%s\nharness=%s\njoined=%s\n' "$name" "$harness" "$(mac_now)" > "$sd/roster/$slug"
}

mac_heartbeat() { if [ -f "$1/roster/$2" ]; then touch "$1/roster/$2"; fi; }

mac_append() {
  local file="$1" sd="$2" block="$3"
  mac_need flock util-linux
  ( flock 9; printf '%s' "$block" >> "$file" ) 9>"$sd/lock"
}
