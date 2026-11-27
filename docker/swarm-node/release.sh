#!/usr/bin/env bash
set -euo pipefail

repository="${1:?registry repository}"
release="${2:?the release tag is required}"
commit="${3:?tested agentihooks commit}"
image="$repository:sha-$commit"

published() {
    local status
    if status="$(docker buildx imagetools inspect "$1" 2>&1 >/dev/null)"; then
        return 0
    elif [[ "$status" != *"not found"* ]]; then
        echo "$status" >&2
        exit 1
    fi
    return 1
}

if [[ "$release" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    tags="$(git ls-remote --tags --refs origin)"
    newest_first="$(sed -n 's#^[0-9a-f]*\trefs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$#\1#p' <<<"$tags" | sort -rV)"
    while read -r -u 3 tag; do
        if [[ "$tag" == "$release" ]]; then
            docker buildx imagetools create --tag "$repository:release" "$image"
            break
        elif published "$repository:${tag#v}"; then
            echo "the newest published release is $tag, not $release; leaving the release tag where it is"
            break
        fi
    done 3<<<"$newest_first"
else
    echo "$release is not a stable release; leaving the release tag where it is"
fi
docker buildx imagetools create --tag "$repository:${release#v}" "$image"
