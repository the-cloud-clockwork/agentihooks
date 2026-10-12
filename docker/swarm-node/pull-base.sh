#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=docker/swarm-node/base-copy.sh
source "$source_dir/base-copy.sh"
if output="$(docker pull --platform linux/amd64 "$copy" 2>&1)"; then
    printf '%s\n' "$output"
    printf 'Pulled the locked worker base from the registry copy %s\n' "$copy"
    if [[ -n "${GITHUB_ENV:-}" ]]; then
        printf 'WORKER_BASE_CONTEXT=%s=docker-image://%s\n' "$base" "$copy" >> "$GITHUB_ENV"
    fi
    exit 0
else
    status=$?
fi
printf '%s\n' "$output" >&2
if ! grep -Eiq 'manifest unknown|not found|denied|unauthorized' <<< "$output"; then
    exit "$status"
fi
printf 'The registry copy is missing; pulling the locked worker base from the public registry\n' >&2
for delay in 15 30 60 120 0; do
    if output="$(docker pull --platform linux/amd64 "$base" 2>&1)"; then
        printf '%s\n' "$output"
        exit 0
    else
        status=$?
    fi
    printf '%s\n' "$output" >&2
    if [[ "$delay" == 0 ]] || ! grep -Eiq '(^|[^[:digit:]])429([^[:digit:]]|$)|toomanyrequests|Too Many Requests' <<< "$output"; then
        exit "$status"
    fi
    printf 'Registry rate limit; retrying the locked worker base in %s seconds\n' "$delay" >&2
    sleep "$delay"
done
