#!/usr/bin/env bash
set -uo pipefail

script="$1"
status="$2"
pane="$3"
unset HISTFILE

read_secret() {
  local __rs_name="$1" __rs_prompt="$2" __rs_value
  if [[ ! "$__rs_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ || "$__rs_name" == __rs_* ]]; then
    echo "read_secret needs a variable name first" >&2
    return 2
  fi
  read -rsp "$__rs_prompt: " __rs_value
  echo
  if [[ -z "$__rs_value" ]]; then
    echo "empty value for $__rs_name" >&2
    return 1
  fi
  printf -v "$__rs_name" '%s' "$__rs_value"
  export "${__rs_name?}"
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

if [[ "$rc" -ne 0 ]]; then
  read -rp "Press Enter to close this pane. " _ || true
fi
herdr pane close "$pane" >/dev/null 2>&1 || true
