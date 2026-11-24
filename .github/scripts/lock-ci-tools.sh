#!/usr/bin/env bash
set -euo pipefail

lock() {
  local name="$1"
  shift
  printf '%s\n' "$@" | uv pip compile - --universal --python-version 3.12 --exclude-newer 2026-11-22T00:00:00Z \
    --upgrade --no-annotate --output-file ".github/ci-tools/$name.lock" \
    --custom-compile-command 'bash .github/scripts/lock-ci-tools.sh'
}

lock parser pyyaml==6.0.3 markdown-it-py==4.2.0 mdurl==0.1.2
lock audit pip-audit==2.10.1 'platformdirs!=4.14.8'
lock semgrep semgrep==1.177.0 pyyaml==6.0.2
lock build build==1.6.1 setuptools==84.2.1 setuptools-scm==10.3.4 wheel==0.48.0
lock browser-crypto playwright==1.63.0 orjson==3.13.0 cryptography==50.0.2
lock lint-tools vulture==2.14 jsonschema==4.25.1 orjson==3.13.0 ruff==0.15.12 packaging==26.3
