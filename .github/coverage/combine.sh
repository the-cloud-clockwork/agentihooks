#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != --downloaded ]]; then
    echo "::error::Usage: combine.sh --downloaded <shards>"
    exit 2
fi
shards="$2"

reports=()
config="$(dirname "$0")/coverage.ini"
for ((shard=1; shard<=shards; shard++)); do
    report=".coverage-shards/coverage-3.12-$shard/.coverage"
    if [[ ! -s "$report" ]]; then
        echo "::error::Missing coverage for shard $shard"
        exit 1
    fi
    reports+=("$report")
    reused=".coverage-shards/coverage-3.12-$shard/reused-coverage/.coverage"
    if [[ -s "$reused" ]]; then reports+=("$reused"); fi
done
for carried in .coverage-shards/branch-*/*/.coverage*; do
    if [[ -s "$carried" ]]; then reports+=("$carried"); fi
done
trap 'kill "${lcov_pid:-}" 2> /dev/null || true' EXIT
lcov_log=$(mktemp)
selected=()
if [[ "${SCOPE:-}" == selected ]]; then selected=(--selected); fi
python "$(dirname "$0")/../../tests/js_lcov.py" --captures .coverage-shards --out lcov.info "${selected[@]}" > "$lcov_log" 2>&1 &
lcov_pid=$!
flat=$(mktemp -d)
flattening=()
for index in "${!reports[@]}"; do
    report="${reports[$index]}"
    taken="$(dirname "$report")/flat-$(basename "$report")"
    if [[ -s "$taken" ]]; then
        mv "$taken" "$flat/.coverage.$index"
        continue
    fi
    python "$(dirname "$0")/flatten.py" "$report" "$flat/.coverage.$index" &
    flattening+=($!)
done
for pid in "${flattening[@]}"; do wait "$pid"; done
python -m coverage combine --rcfile="$config" "$flat"/.coverage.*
workers=$(python "$(dirname "$0")/../../scripts/ci_cpu_quota.py")
python "$(dirname "$0")/parallel_xml.py" "$config" "$workers" coverage.xml
lcov_status=0
wait "$lcov_pid" || lcov_status=$?
cat "$lcov_log"
exit "$lcov_status"
