#!/usr/bin/env bash
set -euo pipefail

source_run="$1"
shards="$2"
if [[ "$source_run" != --downloaded ]]; then
    if [[ "$GITHUB_EVENT_NAME" == push ]]; then
        tree=$(git rev-parse 'HEAD^{tree}')
        for attempt in 1 2 3 4; do
            if passed_run=$(gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=tests-passed-$tree" \
                --jq '[.artifacts[] | select(.expired | not) | select(.workflow_run.head_repository_id == .workflow_run.repository_id)] | first.workflow_run.id // empty'); then
                break
            fi
            if (( attempt == 4 )); then
                exit 1
            fi
            sleep $(( attempt * 5 ))
        done
        source_run="${passed_run:-$source_run}"
    fi
    python "$(dirname "$0")/collect.py" "$source_run" "$shards" .coverage-shards
fi

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
wait "$table_pid"
cat "$table"
