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
done
lcov_log=$(mktemp)
python "$(dirname "$0")/../../tests/js_lcov.py" --captures .coverage-shards --out lcov.info > "$lcov_log" 2>&1 &
lcov_pid=$!
python -m coverage combine --rcfile="$config" --keep "${reports[@]}"
table=$(mktemp)
python -m coverage report --rcfile="$config" > "$table" &
table_pid=$!
python -m coverage xml --rcfile="$config" -o coverage.xml
wait "$table_pid"
cat "$table"
lcov_status=0
wait "$lcov_pid" || lcov_status=$?
cat "$lcov_log"
exit "$lcov_status"
