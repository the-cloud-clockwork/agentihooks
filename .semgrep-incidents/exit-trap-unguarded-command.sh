#!/usr/bin/env bash
set -euo pipefail

cleanup() {
  status=$?
  local finished=yes
  if [[ "$status" != 0 ]]; then
    # ruleid: exit-trap-unguarded-command
    kubectl logs deployment/hive-join --tail=100 > "$out/join.txt" 2>&1
    # ok: exit-trap-unguarded-command
    kubectl describe pods > "$out/pods.txt" 2>&1 || true
  # ok: exit-trap-unguarded-command
  elif kill -0 "$pid" 2>/dev/null; then
    # ruleid: exit-trap-unguarded-command
    kill "$pid"
  fi
  # ok: exit-trap-unguarded-command
  if kill -0 "$pid" 2>/dev/null
  then
    # ruleid: exit-trap-unguarded-command
    kill "$pid"
  fi
  # ok: exit-trap-unguarded-command
  while kill -0 "$pid" 2>/dev/null; do
    # ok: exit-trap-unguarded-command
    sleep 1 || true
  done
  # ok: exit-trap-unguarded-command
  until docker info >/dev/null 2>&1; do
    # ok: exit-trap-unguarded-command
    sleep 1 || true
  # ok: exit-trap-unguarded-command
  done
  # ok: exit-trap-unguarded-command
  kill "${pid:-}" 2>/dev/null || true
  # ruleid: exit-trap-unguarded-command
  rm -r -- "$tls"
  # ruleid: exit-trap-unguarded-command
  rm -r -- "$tls"; kill "$pid" 2>/dev/null || true
  # ruleid: exit-trap-unguarded-command
  [[ -n "$lock" ]] && rm -f "$lock"
  # ok: exit-trap-unguarded-command
  [[ -n "$lock" ]] && rm -f "$lock" || true
  # ruleid: exit-trap-unguarded-command
  docker ps -aq --filter "label=$label" | xargs -r docker rm -f
  # ok: exit-trap-unguarded-command
  docker ps -aq --filter "label=$label" | xargs -r docker rm -f || true
  for job in "$follow" "$rolled"; do
    # ruleid: exit-trap-unguarded-command
    wait "$job"
  done
  # ok: exit-trap-unguarded-command
  kind delete cluster --name "$cluster" || echo "kind could not delete $cluster" >&2
  # ok: exit-trap-unguarded-command
  agentihooks hive remove "$member" --wait "$wait" \
    || echo "hive remove $member failed" >&2
  # ruleid: exit-trap-unguarded-command
  python3 - "$context" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1])
PY
  # ok: exit-trap-unguarded-command
  python3 - "$context" <<'PY' || true
import shutil, sys
shutil.rmtree(sys.argv[1])
PY
  # ok: exit-trap-unguarded-command
  echo "cleaned up $cluster" >&2
  # ok: exit-trap-unguarded-command
  printf '%s\n' "$finished"
  # ok: exit-trap-unguarded-command
  exit "$status"
}
trap cleanup EXIT

quiet() {
  set +e
  # ok: exit-trap-unguarded-command
  docker compose down --volumes
  # ok: exit-trap-unguarded-command
  return 0
}
trap 'quiet' EXIT

relaxed() {
  set +o errexit
  # ok: exit-trap-unguarded-command
  docker compose down --volumes
}
trap relaxed EXIT

guarded() {
  # ok: exit-trap-unguarded-command
  rm -r -- "$tls"
}
trap 'guarded || true' EXIT

interrupted() {
  # ok: exit-trap-unguarded-command
  rm -r -- "$tls"
}
trap interrupted INT TERM

  stop() {
    # ruleid: exit-trap-unguarded-command
    docker compose -f "$fixture" down --volumes --remove-orphans
  }
  trap stop INT TERM EXIT

function release {
  # ruleid: exit-trap-unguarded-command
  docker logout ghcr.io
}
trap "release" EXIT

# ruleid: exit-trap-unguarded-command
finish() { docker compose down --volumes; }
trap -- finish EXIT

trap late EXIT

late() {
  # ruleid: exit-trap-unguarded-command
  rm -rf "$logs"
}

helper() {
  # ok: exit-trap-unguarded-command
  rm -r -- "$tls"
}

# ok: exit-trap-unguarded-command
rm -r -- "$tls"

# ruleid: exit-trap-unguarded-command
trap 'docker compose down --volumes' EXIT
# ruleid: exit-trap-unguarded-command
trap 'rm -f "$out" "$err"' EXIT
# ruleid: exit-trap-unguarded-command
trap "rm -f \"$tmp\"" EXIT
# ruleid: exit-trap-unguarded-command
trap -- 'docker logout ghcr.io' EXIT
# ok: exit-trap-unguarded-command
trap 'docker compose down --volumes >/dev/null 2>&1 || true' EXIT
# ok: exit-trap-unguarded-command
trap 'status=$?; wait "$packages" || true; exit "$status"' EXIT
# ok: exit-trap-unguarded-command
trap 'rm -r -- "$tls"' INT
# ok: exit-trap-unguarded-command
trap - EXIT
