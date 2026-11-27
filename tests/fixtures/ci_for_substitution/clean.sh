#!/usr/bin/env bash
set -euo pipefail

limit=3
for count in {1..3}; do
  printf '%s\n' "$count"
done

for ((count = 1; count <= limit; count++)); do
  printf '%s\n' "$count"
done

for value in $((limit + 1)) "$limit" "${NAMES:-default}"; do
  printf '%s\n' "$value"
done

here="$(dirname "$0")"
for path in "$here"/*.sh; do
  printf '%s %s\n' "$path" "$(basename "$path")"
done

pids="$(jobs -p)"
for job in $pids; do
  wait "$job"
done

for word in "$@"; do
  printf '%s\n' "$word"
done

for item; do
  printf '%s\n' "$item"
done

cat <<EOF
for line in $(printf 'heredoc\n'); do
EOF
printf '%s\n' "for item in \$(date); do"
