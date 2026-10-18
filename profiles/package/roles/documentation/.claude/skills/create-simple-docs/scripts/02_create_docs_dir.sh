#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Create docs/ directory and touch all planned doc files.
# Inputs:   Newline-separated filenames via stdin (e.g. "0A-ARCH.md")
# Outputs:  Created files listed to stdout.
# Idempotency: mkdir -p and touch are idempotent — existing files untouched.

DOCS_DIR="docs"
mkdir -p "$DOCS_DIR"

count=0
while IFS= read -r filename; do
  [[ -z "$filename" ]] && continue
  filepath="$DOCS_DIR/$filename"
  if [[ ! -f "$filepath" ]]; then
    touch "$filepath"
    echo "  created: $filepath"
    ((count++)) || true
  else
    echo "  exists:  $filepath"
  fi
done

echo "Done: $count new files in $DOCS_DIR/"
