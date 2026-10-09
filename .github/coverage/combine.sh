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
python -m coverage combine --rcfile="$config" --keep "${reports[@]}"
table=$(mktemp)
python -m coverage report --rcfile="$config" > "$table" &
table_pid=$!
python -m coverage xml --rcfile="$config" -o coverage.xml
python "$(dirname "$0")/js_lcov.py" --captures .coverage-shards --out lcov.info
wait "$table_pid"
cat "$table"
