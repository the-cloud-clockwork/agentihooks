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
  notes)
    PREVIOUS=$(git tag --list 'v*' --sort=-version:refname | python3 -c 'import re, sys; print(next((t for t in map(str.strip, sys.stdin) if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", t) and t != sys.argv[1]), ""))' "v$3")
    python3 - "$2" "$PREVIOUS" > "$4" <<'PY'
import re
import subprocess
import sys

ref, previous = sys.argv[1], sys.argv[2]


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


if previous:
    paths = git("diff", "--name-only", "--no-renames", "--diff-filter=A", previous, ref, "--", "changelog.d/").split()
else:
    paths = git("ls-tree", "-r", "--name-only", ref, "--", "changelog.d/").split()
order = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
sections = {}
for path in sorted(p for p in paths if p.endswith(".md") and p != "changelog.d/README.md"):
    heading = "Changed"
    for line in git("show", f"{ref}:{path}").splitlines():
        match = re.fullmatch(r"#{2,3}\s+(.+?)\s*", line)
        if match:
            heading = match.group(1)
        elif line.strip():
            sections.setdefault(heading, []).append(line)
out = []
for name in sorted(sections, key=lambda n: (order.index(n) if n in order else len(order), n)):
    out.append(f"### {name}\n\n" + "\n".join(sections[name]) + "\n")
if out:
    print("## Changelog\n\n" + "\n".join(out))
PY
    ;;
  *) exit 1 ;;
esac
