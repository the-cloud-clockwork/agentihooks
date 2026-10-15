#!/usr/bin/env bash
set -euo pipefail

case "$1" in
  version)
    if [[ "${REUSE_TREE:-false}" == true ]]; then
      TAGS=$(git tag --list 'v*' --sort=-version:refname)
      while read -r tag; do
        if [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ "$(git rev-parse "$tag^{tree}")" == "$(git rev-parse HEAD^{tree})" ]]; then
          echo "next=${tag#v}" >> "$GITHUB_OUTPUT"
          echo 'existing=true' >> "$GITHUB_OUTPUT"
          exit 0
        fi
      done <<< "$TAGS"
    fi
    CURRENT=$(git tag --list 'v*' --sort=-version:refname | python3 -c 'import re, sys; print(next(t.strip()[1:] for t in sys.stdin if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+\n?", t)))')
    IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"
    case "$BUMP" in
      major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
      minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
      patch) PATCH=$((PATCH + 1)) ;;
      *) exit 1 ;;
    esac
    echo "current=$CURRENT" >> "$GITHUB_OUTPUT"
    echo "next=${MAJOR}.${MINOR}.${PATCH}" >> "$GITHUB_OUTPUT"
    echo 'existing=false' >> "$GITHUB_OUTPUT"
    ;;
  tag)
    test "$(git rev-parse HEAD)" = "$2"
    git tag "v$3" "$2"
    test "$(git rev-parse HEAD)" = "$2"
    test "$(git rev-parse "v$3^{commit}")" = "$2"
    ;;
  verify)
    python -m pip install build
    python -m build
    python - "$3" <<'PY'
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

wheel, = Path("dist").glob("*.whl")
with zipfile.ZipFile(wheel) as archive:
    metadata, = (name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
    version = Parser().parsestr(archive.read(metadata).decode())["Version"]
assert version == sys.argv[1], version
print(f"Tagged wheel version: {version}")
PY
    test "$(git rev-parse HEAD)" = "$2"
    test -z "$(git status --porcelain --untracked-files=no)"
    ;;
  *) exit 1 ;;
esac
