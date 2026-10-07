#!/usr/bin/env bash
set -euo pipefail

branch="$1"
title="$2"
file="$3"
dry_run="${4:-false}"
: "${GH_TOKEN:?A repository automation token is required to trigger pull request checks}"
[[ "$branch" == automation/* ]]
[[ "$file" == .test_durations || "$file" == pyproject.toml ]]
[[ "$dry_run" == true || "$dry_run" == false ]]

pr=$(gh pr list --repo "$GITHUB_REPOSITORY" --base dev --head "$branch" --state all --json url --jq '.[0].url // empty')
if [[ -z "$pr" ]]; then
    git add "$file"
    if git diff --cached --quiet; then exit 0; fi
    git switch -c "$branch"
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git commit -m "$title"
    git push origin "HEAD:refs/heads/$branch"
    pr=$(gh pr create --repo "$GITHUB_REPOSITORY" --base dev --head "$branch" --title "$title" \
        --body "Automated update from https://github.com/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID. Merge only after the checks pass.")
fi
printf 'pr=%s\n' "$pr" >> "$GITHUB_OUTPUT"
if [[ "$dry_run" == true ]]; then exit 0; fi

for attempt in {1..180}; do
    snapshot=$(gh pr view "$pr" --repo "$GITHUB_REPOSITORY" --json state,headRefOid,statusCheckRollup,mergeStateStatus,mergeCommit)
    state=$(jq -r .state <<< "$snapshot")
    if [[ "$state" == MERGED ]]; then
        jq -r '"merged=" + .mergeCommit.oid' <<< "$snapshot" >> "$GITHUB_OUTPUT"
        exit 0
    fi
    [[ "$state" == OPEN ]]
    head=$(jq -r .headRefOid <<< "$snapshot")
    if [[ "$(jq -r .mergeStateStatus <<< "$snapshot")" == BEHIND ]]; then
        number="${pr##*/}"
        gh api --method PUT "repos/$GITHUB_REPOSITORY/pulls/$number/update-branch" -f expected_head_sha="$head"
    elif jq -e '
        (.statusCheckRollup // []) as $checks |
        any($checks[]; .name == "lint") and
        ([ $checks[] | select((.name // "") | startswith("unit (")) ] | length == 8) and
        any($checks[]; .name == "mutation") and
        all($checks[]; if .__typename == "CheckRun" then .status == "COMPLETED" and .conclusion == "SUCCESS" else .state == "SUCCESS" end)
    ' <<< "$snapshot" > /dev/null; then
        gh pr merge "$pr" --repo "$GITHUB_REPOSITORY" --squash --match-head-commit "$head"
    elif jq -e 'any(.statusCheckRollup[]?; (.status == "COMPLETED" and .conclusion != "SUCCESS") or .state == "FAILURE" or .state == "ERROR")' <<< "$snapshot" > /dev/null; then
        echo "Pull request checks failed: $pr" >&2
        exit 1
    fi
    sleep 10
done
echo "Timed out waiting for successful pull request checks: $pr" >&2
exit 1
