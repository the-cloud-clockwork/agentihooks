#!/usr/bin/env bash
set -euo pipefail

repo="the-cloud-clockwork/agentihooks"
run_id="${1:-}"
if [[ -z "$run_id" ]]; then
    run_id="$(gh api "repos/$repo/actions/workflows/test.yml/runs?event=pull_request&status=completed&per_page=1" --jq '.workflow_runs[0].id // error("No completed pull request Tests run")')"
fi

gh api "repos/$repo/actions/runs/$run_id/jobs?per_page=100" --paginate \
    | jq -es '
        [.[].jobs[] | select(any(.steps[]?; .name == "Operator feature regressions and passing fixtures"))] as $jobs
        | ($jobs | length) > 0 and all($jobs[];
            .conclusion == "success" and
            all(.steps[] | select(.name == "Operator feature regressions and passing fixtures"); .conclusion == "success"))
    '
