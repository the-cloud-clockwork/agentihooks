#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Generate docs/00-INDEX.md from a TSV mapping.
# Inputs:   TSV via stdin: filename<TAB>section_name<TAB>topic_description
#           Lines must be sorted by section number.
# Outputs:  Writes docs/00-INDEX.md
# Idempotency: Overwrites 00-INDEX.md completely — regeneration is the intent.

INDEX="docs/00-INDEX.md"
mkdir -p docs

{
  echo "# Documentation Index"
  echo ""

  current_section=""
  while IFS=$'\t' read -r filename section_name topic; do
    [[ -z "$filename" ]] && continue

    if [[ "$section_name" != "$current_section" ]]; then
      if [[ -n "$current_section" ]]; then
        echo ""
      fi
      echo "## ${section_name}"
      echo "| File | Topic |"
      echo "|------|-------|"
      current_section="$section_name"
    fi

    echo "| [${filename}](${filename}) | ${topic} |"
  done

  echo ""
} > "$INDEX"

echo "Generated: $INDEX"
