#!/usr/bin/env bash
set -euo pipefail

for count in $(seq 3); do
  printf '%s\n' "$count"
done

for count in `seq 3`; do
  printf '%s\n' "$count"
done

for path in "$(dirname "$0")"/*.sh; do
  printf '%s\n' "$path"
done

for name in "${NAMES:-$(hostname)}"; do
  printf '%s\n' "$name"
done

for line in $(< /etc/hostname); do
  printf '%s\n' "$line"
done

for word in first \
  $(printf 'second\n'); do
  printf '%s\n' "$word"
done

if true; then for job in $(jobs -p); do wait "$job"; done; fi
