#!/usr/bin/env bash
set -euo pipefail

base=$1
head=$2
paths=(
  deploy/helm/agentihooks-swarm/
  Dockerfile
  .dockerignore
  pyproject.toml
  .github/workflows/helm-kind.yml
  .github/workflows/test.yml
  scripts/swarm/controller.py
  scripts/swarm/lease.py
  scripts/hive/
)
changed="$(git diff --name-only "$base...$head" -- "${paths[@]}")"
if [[ -n $changed ]]; then
  printf 'chart proof due for:\n%s\n' "$changed" >&2
  echo due=true
else
  printf 'no chart, swarm image or chart proof file changed\n' >&2
  echo due=false
fi
