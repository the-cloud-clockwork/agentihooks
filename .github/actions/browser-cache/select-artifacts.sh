#!/usr/bin/env bash
set -euo pipefail

browser=false
if [[ -z "$BASE" || "$BASE" =~ ^0+$ ]] || ! git rev-parse --verify "$BASE^{commit}" >/dev/null 2>&1; then
  browser=true
else
  changed="$(git diff --no-renames --name-only "$BASE" HEAD)"
  while IFS= read -r path; do
    case "$path" in
      scripts/swarm_ledger/artifact_sanity.py|scripts/swarm_ledger/template.html|scripts/swarm_ledger/palette.css|scripts/swarm_ledger/tooltips.js|scripts/swarm_ledger/static/*|tests/fixtures/artifacts/*|tests/swarm_ledger/test_artifact*|.github/actions/browser-cache/*|.github/workflows/test.yml|.github/test-excludes.txt|pyproject.toml)
        browser=true
        break
        ;;
    esac
  done <<< "$changed"
fi
echo "browser=$browser" >> "$GITHUB_OUTPUT"
