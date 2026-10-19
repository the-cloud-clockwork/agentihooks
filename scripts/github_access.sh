#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
policy="$root/.github/security/access.json"
organization="$(jq -r .organization "$policy")"
operator="$(jq -r .operator "$policy")"
group="$(jq -r .runner_group.id "$policy")"
mode="${1:-check}"
failures=0
checks=0

case "$mode" in
  check) ;;
  apply)
    if [[ -n "${AGENTIHOOKS_AGENT_NAME:-}${AGENTIHOOKS_SWARM_TASK:-}${GITHUB_ACTIONS:-}" ]]; then
      echo 'Only the operator may apply GitHub access settings' >&2
      exit 1
    fi
    [[ "$(gh api user --jq .login)" == "$operator" ]] || {
      echo 'Apply requires the operator account' >&2
      exit 1
    }
    while IFS= read -r name; do
      prefix="repos/$organization/$name/actions/permissions"
      jq .contributor_approval "$policy" | gh api --method PUT "$prefix/fork-pr-contributor-approval" --input - > /dev/null
      jq .workflow_permissions "$policy" | gh api --method PUT "$prefix/workflow" --input - > /dev/null
    done < <(jq -r '.repositories[]' "$policy")
    jq '{allows_public_repositories: .runner_group.allows_public_repositories}' "$policy" |
      gh api --method PATCH "orgs/$organization/actions/runner-groups/$group" --input - > /dev/null
    installed="$(gh api "orgs/$organization/installations?per_page=100" --paginate | jq -s '[.[].installations[]]')"
    unapproved="$(jq -r --argjson allowed "$(jq '[.installed_apps[].app_id]' "$policy")" \
      '.[] | select(.app_id as $id | ($allowed | index($id)) == null) | [.app_slug, .html_url] | @tsv' <<< "$installed")"
    if [[ -n "$unapproved" ]]; then
      while IFS=$'\t' read -r app url; do
        printf 'Uninstall %s: %s\n' "$app" "$url"
        xdg-open "$url" < /dev/null
      done <<< "$unapproved"
      read -r -p 'Uninstall those apps in the browser, then press Enter to verify: ' confirmation
    fi
    ;;
  *) echo 'Usage: bash scripts/github_access.sh check|apply' >&2; exit 2 ;;
esac

check_json() {
  local label="$1" actual="$2" expected="$3" projection="$4"
  checks=$((checks + 1))
  if ! jq -e --argjson expected "$expected" "($projection) == \$expected" <<< "$actual" > /dev/null; then
    echo "DRIFT $label" >&2
    failures=$((failures + 1))
  fi
  printf '%s: %s\n' "$label" "$(jq -c "$projection" <<< "$actual")"
}

actual="$(gh api "orgs/$organization")"
check_json 'organization default permission' "$actual" '"read"' '.default_repository_permission'
actual="$(gh api "orgs/$organization/members?per_page=100" --paginate | jq -s add)"
check_json 'organization members' "$actual" "$(jq -cn --arg operator "$operator" '[$operator]')" 'map(.login) | sort'
actual="$(gh api "orgs/$organization/installations?per_page=100" --paginate | jq -s '[.[].installations[]]')"
check_json 'installed GitHub apps' "$actual" "$(jq '.installed_apps | sort_by(.app_id)' "$policy")" 'map({app_id, app_slug}) | sort_by(.app_id)'

actual="$(gh api "orgs/$organization/actions/permissions/fork-pr-contributor-approval")"
check_json 'organization contributor approval' "$actual" "$(jq .contributor_approval "$policy")" '{approval_policy}'
public_repositories="$(gh api "orgs/$organization/repos?type=public&per_page=100" --paginate | jq -es 'add | map(.name) | unique | select(length > 0)')"
while IFS= read -r name; do
  actual="$(gh api "repos/$organization/$name/actions/permissions/fork-pr-contributor-approval")"
  check_json "$name contributor approval" "$actual" "$(jq .contributor_approval "$policy")" '{approval_policy}'
done < <(jq -r '.[]' <<< "$public_repositories")

while IFS= read -r name; do
  prefix="repos/$organization/$name"
  actual="$(gh api "$prefix/actions/permissions/workflow")"
  check_json "$name workflow token" "$actual" "$(jq .workflow_permissions "$policy")" '{default_workflow_permissions, can_approve_pull_request_reviews}'
  actual="$(gh api "$prefix/collaborators?per_page=100" --paginate | jq -s add)"
  check_json "$name writers" "$actual" "$(jq -cn --arg operator "$operator" '[$operator]')" '[.[] | select(.permissions.push == true or .permissions.admin == true) | .login] | sort'
  check_json "$name administrators" "$actual" "$(jq -cn --arg operator "$operator" '[$operator]')" '[.[] | select(.permissions.admin == true) | .login] | sort'
  actual="$(gh api "$prefix/teams?per_page=100" --paginate | jq -s add)"
  check_json "$name teams" "$actual" '[]' '.'
  actual="$(gh api "$prefix/keys?per_page=100" --paginate | jq -s add)"
  check_json "$name deploy keys" "$actual" '[]' '.'
done < <(jq -r '.repositories[]' "$policy")

actual="$(gh api "orgs/$organization/actions/runner-groups/$group")"
check_json 'Default runner public access' "$actual" "$(jq .runner_group "$policy")" '{id, name, allows_public_repositories}'

public_group="$(jq -r .public_runner_group.id "$policy")"
actual="$(gh api "orgs/$organization/actions/runner-groups/$public_group")"
check_json 'Anton open source runner group' "$actual" "$(jq '.public_runner_group | del(.repositories)' "$policy")" '{id, name, visibility, allows_public_repositories, restricted_to_workflows, selected_workflows}'
actual="$(gh api "orgs/$organization/actions/runner-groups/$public_group/repositories?per_page=100" --paginate | jq -s '[.[].repositories[]]')"
check_json 'Anton open source runner repositories' "$actual" "$(jq '.public_runner_group.repositories | sort' "$policy")" 'map(.name) | sort'

echo "$checks access checks, $failures drift findings"
if [[ "$failures" -ne 0 ]]; then
  exit 1
fi
echo 'GitHub access controls verified'
