#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Validate that CWD looks like a project root.
# Inputs:   None (uses CWD)
# Outputs:  Exit 0 if valid, exit 1 with error message if not.
# Idempotency: Read-only check, always safe to re-run.

MARKERS=(
  src lib packages stacks k8s app cmd internal
  Makefile package.json pyproject.toml Cargo.toml go.mod
  docker-compose.yml compose.yml .git
)

found=0
for marker in "${MARKERS[@]}"; do
  if [[ -e "$marker" ]]; then
    found=1
    break
  fi
done

if [[ "$found" -eq 0 ]]; then
  echo "ERROR: CWD does not look like a project root." >&2
  echo "Expected at least one of: ${MARKERS[*]}" >&2
  exit 1
fi

echo "OK: Project root validated at $(pwd)"
