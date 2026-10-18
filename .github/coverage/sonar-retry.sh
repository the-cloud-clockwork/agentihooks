#!/usr/bin/env bash
set -euo pipefail

unless=""
if [[ "${1:-}" == --unless ]]; then
  unless="$2"
  shift 2
fi
label="$1"
[[ "$2" == -- ]]
shift 2

transient='returned error: (401|403|408|429|5[0-9][0-9])|Failed to connect|Connection refused|Connection reset|timed out|Empty reply from server|Recv failure|Fail to request url|Call to URL .* failed|unexpected end of stream|SocketTimeoutException|Error status returned by url .*: (401|403|408|429|5[0-9][0-9])|failed with HTTP (401|403|408|429|5[0-9][0-9])|HTTP code (401|403|408|429|5[0-9][0-9])'
authorization='returned error: (401|403)|Error status returned by url .*: (401|403)|failed with HTTP (401|403)|HTTP code (401|403)'
upload='Failed to upload report: Error 5[0-9][0-9] on https?://[^[:space:]]*/api/ce/submit([?[:space:]]|$)'
deadline="${SONAR_RETRY_SECONDS:-180}"
out=$(mktemp)
err=$(mktemp)
trap 'rm -f "$out" "$err"' EXIT
start=$SECONDS
waited=0
wait=2
attempt=0
denials=0
upload_attempt=0

while true; do
  attempt=$((attempt + 1))
  status=0
  "$@" > "$out" 2> "$err" || status=$?
  if ((status == 0)); then
    cat "$err" >&2
    cat "$out"
    exit 0
  fi
  upload_reason=$(cat "$out" "$err" | grep -E "$upload" | tail -n1 || true)
  reason=${upload_reason:-$(cat "$out" "$err" | grep -E "$transient" | tail -n1 || true)}
  if [[ (-z "$reason" && "$upload_attempt" == 0) || (-n "$unless" && -e "$unless") ]]; then
    cat "$err" >&2
    cat "$out"
    exit "$status"
  fi
  spent=$((SECONDS - start > waited ? SECONDS - start : waited))
  if [[ -n "$upload_reason" && "$upload_attempt" == 0 ]]; then
    upload_attempt="$attempt"
    wait=2
  fi
  if [[ "$reason" =~ $authorization ]]; then
    denials=$((denials + 1))
    wait=2
  fi
  if ((upload_attempt > 0 && attempt > upload_attempt || denials == 2 || spent + wait > deadline)); then
    cat "$err" >&2
    cat "$out" >&2
    message="$label failed $attempt times over ${spent}s: $reason. The Sonar server or its Access proxy did not answer; this is not a code verdict."
    if ((upload_attempt > 0)); then
      message="$message Sonar report upload failed during a Sonar server outage (infrastructure)."
    fi
    echo "::error title=Sonar server unavailable::$message" >&2
    [[ -z "${GITHUB_STEP_SUMMARY:-}" ]] || echo "Sonar server unavailable: $message" >> "$GITHUB_STEP_SUMMARY"
    exit "$status"
  fi
  cat "$out" "$err" >&2
  echo "::warning title=Sonar server retry::$label attempt $attempt: $reason; retrying in ${wait}s" >&2
  sleep "$wait"
  waited=$((waited + wait))
  wait=$((wait * 2 > 30 ? 30 : wait * 2))
done
