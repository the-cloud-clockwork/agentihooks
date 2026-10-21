#!/usr/bin/env bash
set -euo pipefail

label="$1"
[[ "$2" == -- ]]
shift 2

outage='HTTP status server error|HTTP status client error \((408|429) |error sending request for url|tcp connect error|Connection (refused|reset)|dns error|timed out|deadline has elapsed'
deadline="${PYPI_RETRY_SECONDS:-150}"
log=$(mktemp)
trap 'rm -f "$log"' EXIT
waited=0
wait=5
attempt=0

while true; do
  attempt=$((attempt + 1))
  status=0
  "$@" > "$log" 2>&1 || status=$?
  cat "$log" >&2
  ((status != 0)) || exit 0
  reason=$(grep -E "$outage" "$log" | tail -n1 | sed 's/^[[:space:]]*//' || true)
  [[ -n "$reason" ]] || exit "$status"
  if ((waited >= deadline)); then
    message="$label failed $attempt times over ${waited}s: $reason. PyPI or the network did not answer; this is not a code verdict."
    echo "::error title=Python package index unavailable::$message" >&2
    [[ -z "${GITHUB_STEP_SUMMARY:-}" ]] || echo "Python package index unavailable: $message" >> "$GITHUB_STEP_SUMMARY"
    exit "$status"
  fi
  echo "::warning title=Python package index retry::$label attempt $attempt: $reason; retrying in ${wait}s" >&2
  sleep "$wait"
  waited=$((waited + wait))
  wait=$((wait * 2 > 30 ? 30 : wait * 2))
done
