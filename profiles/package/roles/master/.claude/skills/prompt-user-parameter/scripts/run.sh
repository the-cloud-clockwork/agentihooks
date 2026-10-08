#!/usr/bin/env bash
set -uo pipefail

script="$1"
status="$2"
pane="$3"
unset HISTFILE

read_secret() {
  local name="$1" prompt="$2" value
  read -rsp "$prompt: " value
  echo
  if [[ -z "$value" ]]; then
    echo "empty value for $name" >&2
    return 1
  fi
  printf -v "$name" '%s' "$value"
  export "${name?}"
}
export -f read_secret

clear 2>/dev/null
bash -e "$script"
rc=$?
rm -f "$script"

if [[ "$rc" -eq 0 ]]; then
  printf 'DONE\n' >"$status.tmp"
  printf '\n\033[1;32m==== DONE: %s ====\033[0m\n' "$(basename "$script")"
else
  printf 'ERROR %s\n' "$rc" >"$status.tmp"
  printf '\n\033[1;31m==== ERROR exit %s: %s ====\033[0m\n' "$rc" "$(basename "$script")"
fi
mv "$status.tmp" "$status"

read -rp "Press Enter to close this pane. " _ || true
herdr pane close "$pane" >/dev/null 2>&1 || true
