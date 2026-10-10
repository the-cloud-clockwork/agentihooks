#!/usr/bin/env bash
set -euo pipefail

image="${1:?worker image to scan}"
output="$(realpath -m "${2:?scan evidence directory}")"
scanner="aquasec/trivy:0.58.1"
mkdir -p "$output"
rm -f "$output/secrets.json"
status=0
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro -v "$output:/out" "$scanner" \
    image --quiet --scanners secret --image-config-scanners secret --exit-code 1 \
    --format json --output /out/secrets.json "$image" > "$output/scan.log" 2>&1 || status=$?
if [[ ! -s "$output/secrets.json" ]]; then
    echo "credential scan did not complete for $image (exit $status)" >&2
    cat "$output/scan.log" >&2
    exit 2
fi
python3 - "$output/secrets.json" > "$output/findings.txt" <<'PY'
import json, sys
from pathlib import Path
for result in json.loads(Path(sys.argv[1]).read_text()).get("Results") or []:
    for secret in result.get("Secrets") or []:
        print(secret["RuleID"], result["Target"])
PY
found="$(wc -l < "$output/findings.txt")"
if (( found > 0 )); then
    echo "$found credential findings in $image:" >&2
    cat "$output/findings.txt" >&2
    exit 1
fi
if (( status != 0 )); then
    echo "credential scan failed for $image (exit $status)" >&2
    cat "$output/scan.log" >&2
    exit 2
fi
echo "no credentials found in $image"
