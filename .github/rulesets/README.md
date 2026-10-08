# Repository rulesets

These JSON files are update payloads for the existing repository rulesets,
using the [GitHub REST API](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset).
They contain writable settings only. Both rulesets enforce deletion and force
push protection, linear history and pull requests without bypass actors.
Dev requires the exact `Gate — Required` check from the Tests workflow and
allows squash merges with zero required approvals. Instead of requiring each
branch to be up to date, dev merges through a merge queue: the Tests workflow
runs on `merge_group`, so unit, lint, Sonar and the swarm image smoke grade
the merged tree before it lands. Mutation stays on the pull request head.
Once the queue is active, `gh pr merge --squash` adds the pull request to the
queue; it lands when the queue run passes the gate.
Main keeps its existing branch match and rules; only its bypass list changes.

The operator applies the payloads after the merge queue change to the Tests
workflow has merged into dev. Agents cannot mutate remote
rulesets. From a checkout containing the reviewed payloads:

```bash
gh api \
  --method PUT \
  repos/The-Cloud-Clockwork/agentihooks/rulesets/16090886 \
  --input .github/rulesets/dev-no-delete.json
gh api \
  --method PUT \
  repos/The-Cloud-Clockwork/agentihooks/rulesets/15122747 \
  --input .github/rulesets/main-prod-lockdown.json
```

Verify the effective rules and bypass lists:

```bash
gh api repos/The-Cloud-Clockwork/agentihooks/rules/branches/dev
gh api repos/The-Cloud-Clockwork/agentihooks/rulesets/16090886 --jq .bypass_actors
gh api repos/The-Cloud-Clockwork/agentihooks/rulesets/15122747 --jq .bypass_actors
gh api repos/The-Cloud-Clockwork/agentihooks/rules/branches/dev \
  --jq '.[] | select(.type == "pull_request") | .parameters.require_code_owner_review'
```

The dev `pull_request` rule must carry `require_code_owner_review` true, so a
pull request touching a path in `.github/CODEOWNERS` waits for owner review.
The dev response must include `pull_request`, `required_linear_history`,
`merge_queue` and `required_status_checks` requiring `Gate — Required` with
`strict_required_status_checks_policy` false; both bypass lists must be empty.
The first pull request merged through the queue must show a Tests run on the
`merge_group` event whose `Gate — Required` passed. A controlled direct push attempt must be rejected by GitHub's pull
request rule. An unchanged ref or a dry run does not exercise that rule.
Record the rejection and the green and planted red CI run IDs on the task
and pull request before claiming the live controls are proven.

## Release environments and version tags

`version-tags.json` is a create payload for a tag ruleset: a `v*` tag cannot
be moved, force pushed or deleted. Creation stays open because the release
workflow pushes its tag with the workflow token, which a ruleset cannot list
as a bypass actor.

`../environments/*.json` hold the deployment environments. `release` deploys
only from `dev`, so `release.yml` dispatched from any other ref is refused
before a step runs. `pypi` deploys only from `main` and `v*` tags and keeps
the operator as required reviewer; `publish-pypi.yml` uses no other
environment. Apply as the operator:

```bash
gh api \
  --method POST \
  repos/The-Cloud-Clockwork/agentihooks/rulesets \
  --input .github/rulesets/version-tags.json
for env in release pypi; do
  jq .environment ".github/environments/$env.json" | gh api \
    --method PUT \
    "repos/The-Cloud-Clockwork/agentihooks/environments/$env" \
    --input -
  jq -c '.deployment_branch_policies[]' ".github/environments/$env.json" |
    while read -r policy; do
      gh api \
        --method POST \
        "repos/The-Cloud-Clockwork/agentihooks/environments/$env/deployment-branch-policies" \
        --input - <<< "$policy"
    done
done
```

A live policy not in the file is removed by its id. The `main` policy the
operator added to `release` for the old publish gate goes once this merges:

```bash
gh api \
  --method DELETE \
  repos/The-Cloud-Clockwork/agentihooks/environments/release/deployment-branch-policies/62304415
```

Verify:

```bash
gh api repos/The-Cloud-Clockwork/agentihooks/environments/release/deployment-branch-policies --jq '.branch_policies[]|{name,type}'
gh api repos/The-Cloud-Clockwork/agentihooks/environments/pypi/deployment-branch-policies --jq '.branch_policies[]|{name,type}'
gh api repos/The-Cloud-Clockwork/agentihooks/rulesets --jq '.[]|select(.target=="tag")|{id,name,enforcement}'
```
