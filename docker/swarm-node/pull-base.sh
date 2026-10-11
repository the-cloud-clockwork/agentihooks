#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="$(jq -er .base_image "$source_dir/versions.lock")"
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
