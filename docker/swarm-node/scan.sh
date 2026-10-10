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
if [[ ! -s "$output/secrets.json" ]] || (( status > 1 )); then
    echo "credential scan did not complete for $image (exit $status)" >&2
    cat "$output/scan.log" >&2
    exit 2
fi
python3 - "$output" <<'PY'
import json, re, sys
from pathlib import Path
output = Path(sys.argv[1])
described = re.compile(r"opt/venv/lib/python3\.\d+/site-packages/[^/]+\.dist-info/METADATA$")
findings, examples = [], []
for result in json.loads((output / "secrets.json").read_text()).get("Results") or []:
    for secret in result.get("Secrets") or []:
        line = f"{secret['RuleID']} {result['Target']}\n"
        example = secret["RuleID"] == "jwt-token" and described.search(result["Target"])
        (examples if example else findings).append(line)
(output / "findings.txt").write_text("".join(findings))
(output / "package-examples.txt").write_text("".join(examples))
PY
found="$(wc -l < "$output/findings.txt")"
if (( found > 0 )); then
    echo "$found credential findings in $image:" >&2
    cat "$output/findings.txt" >&2
    exit 1
fi
if (( status == 1 )) && [[ ! -s "$output/package-examples.txt" ]]; then
    echo "credential scan failed for $image: exit 1 without findings" >&2
    exit 2
fi
echo "no credentials found in $image"
