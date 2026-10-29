#!/usr/bin/env bash
set -euo pipefail

uv pip compile pyproject.toml --extra dev --extra all --universal \
  --constraint <(sed '/^--constraint /d' .github/test-constraints.txt) \
  --output-file .github/test-requirements.lock --no-annotate \
  --custom-compile-command 'bash .github/scripts/lock-test-deps.sh' "$@"
