#!/usr/bin/env bash
set -euo pipefail

state="$RUNNER_TEMP/retry-upload-$(printf '%s' "$ARTIFACT" | sha256sum | cut -c1-16)-$ATTEMPT"

case "$1" in
  arm)
    rm -f "$state.fired"
    nohup bash "$0" fire < /dev/null > /dev/null 2>&1 &
    echo "$!" > "$state.pid"
    ;;
  fire)
    sleep "$ATTEMPT_SECONDS"
    pgrep -f "$UPLOAD_PROCESS" > /dev/null || exit 0
    touch "$state.fired"
    pkill -TERM -f "$UPLOAD_PROCESS" || true
    sleep 10
    pkill -KILL -f "$UPLOAD_PROCESS" || true
    ;;
  disarm)
    if [[ -f "$state.pid" ]]; then
      kill "$(cat "$state.pid")" 2> /dev/null || true
      rm -f "$state.pid"
    fi
    if [[ -f "$state.fired" ]]; then
      rm -f "$state.fired"
      echo "::warning title=Retried upload::Attempt $ATTEMPT to upload $ARTIFACT exceeded ${ATTEMPT_SECONDS}s and was ended."
    fi
    ;;
esac
