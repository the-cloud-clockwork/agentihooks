#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
logs="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
scripts=("$@")
if [[ "${SWARM_SMOKE_CACHE_EXPORT:-}" == true ]]; then
  scripts+=(cache-export)
fi
scenario() {
  local start=$SECONDS status=0
  local -a command=(bash "$1")
  if [[ "$1" == cache-export ]]; then
    command=(docker buildx build --output type=cacheonly
      --cache-to type=gha,version=2,mode=max,scope=swarm-smoke,compression=zstd,force-compression=true .)
  fi
  "${command[@]}" >"$2" 2>&1 || status=$?
  printf '%s %s\n' "$status" "$((SECONDS - start))" >"$2.result"
}
outcome() {
  if ! read -r status elapsed <"$logs/$1.log.result" 2>/dev/null; then
    status="no result"
    elapsed="?"
  fi
}
for i in "${!scripts[@]}"; do
  scenario "${scripts[i]}" "$logs/$i.log" &
done
wait
failed=0
for i in "${!scripts[@]}"; do
  outcome "$i"
  verdict=passed
  if [[ $status != 0 ]]; then
    verdict="failed with exit $status"
    failed=1
  fi
  printf '::group::%s %s in %ss\n' "${scripts[i]}" "$verdict" "$elapsed"
  cat "$logs/$i.log" 2>/dev/null || true
  printf '::endgroup::\n'
  if [[ $status != 0 ]]; then
    printf '::error title=%s::%s in %ss\n' "${scripts[i]}" "$verdict" "$elapsed"
  fi
done
for i in "${!scripts[@]}"; do
  outcome "$i"
  printf '%-45s exit %s, %ss\n' "${scripts[i]}" "$status" "$elapsed"
done
exit "$failed"
