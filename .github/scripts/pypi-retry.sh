#!/usr/bin/env bash
set -euo pipefail

label="$1"
[[ "$2" == -- ]]
shift 2

outage='^[[:space:]]*(cause|Caused by): .*(HTTP status server error|HTTP status client error \((408|429) |tcp connect error|Connection (refused|reset)|dns error|operation timed out|deadline has elapsed)'
deadline="${PYPI_RETRY_SECONDS:-150}"
out=$(mktemp)
err=$(mktemp)
trap 'rm -f "$out" "$err"' EXIT
waited=0
wait=5
attempt=0

while true; do
  attempt=$((attempt + 1))
  status=0
  "$@" > "$out" 2> "$err" || status=$?
  cat "$err" >&2
  cat "$out"
  ((status != 0)) || exit 0
  reason=$(cat "$out" "$err" | grep -E "$outage" | tail -n1 | sed 's/^[[:space:]]*//' || true)
  [[ -n "$reason" ]] || exit "$status"
  if ((waited >= deadline)); then
    message="$label failed after $attempt attempts and ${waited}s of backoff: $reason. PyPI or the network did not answer; this is not a code verdict."
    echo "::error title=Python package index unavailable::$message" >&2
    [[ -z "${GITHUB_STEP_SUMMARY:-}" ]] || echo "Python package index unavailable: $message" >> "$GITHUB_STEP_SUMMARY"
    exit "$status"
  fi
  echo "::warning title=Python package index retry::$label attempt $attempt: $reason; retrying in ${wait}s" >&2
  sleep "$wait"
  waited=$((waited + wait))
  wait=$((wait * 2 > 30 ? 30 : wait * 2))
done
