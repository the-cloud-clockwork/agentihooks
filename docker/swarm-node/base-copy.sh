#!/usr/bin/env bash
set -euo pipefail

base="$(jq -er .base_image "$source_dir/versions.lock")"
digest="${base##*@}"
copy_repository=ghcr.io/the-cloud-clockwork/agentihooks-worker-base
copy="$copy_repository@$digest"
