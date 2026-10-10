#!/usr/bin/env bash
set -euo pipefail

base=$1
head=$2
paths=(
  deploy/helm/agentihooks-swarm/
  .github/workflows/helm-kind.yml
  .github/workflows/test.yml
  Dockerfile
  .dockerignore
  pyproject.toml
  README.md
  docker/swarm/requirements.lock
  hooks/
  scripts/
  profiles/
  media/
  docs/swarm-v2/schemas/
  tests/chart_workers.py
)
changed="$(git diff --name-only "$base...$head" -- "${paths[@]}")"
if [[ -n $changed ]]; then
  printf 'chart proof due for:\n%s\n' "$changed" >&2
  echo due=true
else
  printf 'no chart, swarm image or chart proof file changed\n' >&2
  echo due=false
fi
