#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: open.sh <script> [title]" >&2
  exit 2
fi
script="$(readlink -f "$1")"
if [[ ! -f "$script" ]]; then
  echo "no such script: $1" >&2
  exit 2
fi
scratch="$HOME/scratchpad"
if [[ "$script" != "$(readlink -f "$scratch")"/* ]]; then
  echo "refused: $script is outside $scratch" >&2
  exit 2
fi
title="${2:-operator input}"
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
status="${script%.*}.status"
rm -f "$status"

if [[ -z "${HERDR_PANE_ID:-}" ]]; then
  echo "refused: run from inside a herdr pane (HERDR_PANE_ID is unset)" >&2
  exit 2
fi
split="$(herdr pane split --pane "$HERDR_PANE_ID" --direction right --cwd "$(dirname "$script")")"
pane="$(printf '%s' "$split" | python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')"
herdr pane rename "$pane" "$title" >/dev/null 2>&1 || true
herdr pane run "$pane" "bash $(printf '%q' "$here/run.sh") $(printf '%q' "$script") $(printf '%q' "$status") $(printf '%q' "$pane")" >/dev/null

echo "pane=$pane"
echo "status=$status"
