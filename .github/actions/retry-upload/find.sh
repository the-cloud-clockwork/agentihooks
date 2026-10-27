#!/usr/bin/env bash
set -euo pipefail

IFS=
shopt -s globstar nullglob
[[ "$INCLUDE_HIDDEN" == true ]] && shopt -s dotglob

visible() {
  [[ "$INCLUDE_HIDDEN" == true || "$(basename "$1")" != .* ]]
}

holds_a_file() {
  if [[ -f "$1" ]]; then
    return 0
  fi
  [[ -d "$1" ]] || return 1
  if [[ "$INCLUDE_HIDDEN" == true ]]; then
    [[ -n "$(find "$1" -mindepth 1 -type f -print -quit)" ]]
  else
    [[ -n "$(find "$1" -mindepth 1 -name '.*' -prune -o -type f -print -quit)" ]]
  fi
}

found=false
while read -r pattern; do
  [[ -z "$pattern" || "$pattern" == '!'* ]] && continue
  [[ "$pattern" == '~'* ]] && pattern="$HOME${pattern:1}"
  for match in $pattern; do
    if visible "$match" && holds_a_file "$match"; then
      found=true
      break 2
    fi
  done
done <<< "$PATHS"

echo "found=$found" >> "$GITHUB_OUTPUT"
[[ "$found" == true ]] && exit 0
case "$IF_NO_FILES_FOUND" in
  error)
    echo "::error title=Retried upload::No files match the path of $ARTIFACT; a missing file cannot recover, so it is not retried."
    exit 1
    ;;
  ignore)
    echo "No files match the path of $ARTIFACT; nothing uploaded."
    ;;
  *)
    echo "::warning title=Retried upload::No files match the path of $ARTIFACT; nothing uploaded."
    ;;
esac
