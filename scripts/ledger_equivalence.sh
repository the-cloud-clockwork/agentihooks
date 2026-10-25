#!/usr/bin/env bash
set -euo pipefail

# Run from the workspace holding the base and head checkouts; the grading logic is this file's own checkout.
grader=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
replays=$grader/tests/swarm_ledger

replay() {
  name=$1
  shift
  if ! "$@" > "$RUNNER_TEMP/$name.log" 2>&1; then
    echo "::group::$name replay failed"
    cat "$RUNNER_TEMP/$name.log"
    echo "::endgroup::"
    return 1
  fi
}

settle() {
  failed=0
  for job in $(jobs -p); do
    wait "$job" || failed=1
  done
  return "$failed"
}

plant() {
  cp -a head fault-store
  cp -a head fault-chat
  python - <<'PY'
from pathlib import Path
for path, needle, planted in (
    (
        Path('fault-store/scripts/swarm_ledger/repository/mutation.py'),
        'rejected = core.apply_changes(doc, changes or [], ctx)',
        'rejected = core.apply_changes(doc, [], ctx)',
    ),
    (
        Path('fault-chat/scripts/swarm_ledger/static/js/chat.js'),
        'return to && !text.startsWith("@") ? `@${to} ${text}` : text;',
        'return text;',
    ),
):
    source = path.read_text()
    assert source.count(needle) == 1
    path.write_text(source.replace(needle, planted))
PY
}

store() {
  replay base python "$replays/repository_replay.py" "$PWD/base" "$RUNNER_TEMP/base-ledger" base.json &
  replay head python "$replays/repository_replay.py" "$PWD/head" "$RUNNER_TEMP/head-ledger" head.json &
  replay fault python "$replays/repository_replay.py" "$PWD/fault-store" "$RUNNER_TEMP/fault-ledger" fault.json &
  settle
  cmp base.json head.json
  if cmp -s base.json fault.json; then
    echo 'FAIL: planted checkbox fault escaped the equivalence check'
    exit 1
  fi
  echo 'PASS: identical base and head; planted checkbox fault rejected'
}

page() {
  replay base-page python -I "$replays/page_replay.py" "$PWD/base" "$RUNNER_TEMP/base-page" base-page.json &
  replay head-page python -I "$replays/page_replay.py" "$PWD/head" "$RUNNER_TEMP/head-page" head-page.json &
  replay fault-page python -I "$replays/page_replay.py" "$PWD/fault-chat" "$RUNNER_TEMP/fault-page" fault-page.json &
  settle
  cmp base-page.json head-page.json
  if cmp -s base-page.json fault-page.json; then
    echo 'FAIL: planted chat fault escaped the page equivalence check'
    exit 1
  fi
  echo 'PASS: identical base and head pages; planted chat fault rejected'
}

case "${1:-}" in
  plant | store | page) "$1" ;;
  *)
    echo "usage: $0 plant|store|page" >&2
    exit 2
    ;;
esac
