#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
logs="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
scenario() {
  local start=$SECONDS status=0
  bash "$1" >"$2" 2>&1 || status=$?
  printf '%s %s\n' "$status" "$((SECONDS - start))" >"$2.result"
}
for script in "$@"; do
  scenario "$script" "$logs/$(basename "$script").log" &
done
wait
failed=0
for script in "$@"; do
  log="$logs/$(basename "$script").log"
  read -r status elapsed <"$log.result"
  verdict=passed
  if [[ $status != 0 ]]; then
    verdict="failed with exit $status"
    failed=1
  fi
  printf '::group::%s %s in %ss\n' "$script" "$verdict" "$elapsed"
  cat "$log"
  printf '::endgroup::\n'
  if [[ $status != 0 ]]; then
    printf '::error title=%s::%s in %ss\n' "$script" "$verdict" "$elapsed"
  fi
done
for script in "$@"; do
  read -r status elapsed <"$logs/$(basename "$script").log.result"
  printf '%-45s exit %s, %ss\n' "$script" "$status" "$elapsed"
done
exit "$failed"
