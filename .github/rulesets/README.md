# Repository rulesets

These JSON files are update payloads for the existing repository rulesets,
using the [GitHub REST API](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset).
They contain writable settings only. `dev-no-delete` and `main-prod-lockdown`
enforce deletion and force push protection, linear history and pull requests. `main-prod-lockdown` has no
bypass actors. `dev-no-delete` lists two in `pull_request` mode, the repository
admin role (`RepositoryRole` 5) and the agenticore GitHub App (`Integration`,
its app id from the OpenBao `k8s/agenticore` field `GITHUB_APP_ID`), because
GitHub honours a merge queue jump only for a bypass actor of the ruleset that
holds the `merge_queue` rule. Pull request mode still lets those actors bypass
linear history and the required check on a pull request merge, so
`dev-history.json` repeats deletion, force push, linear history and the
`Gate — Required` check on dev with no bypass actors. A jump moves a pull
request ahead in the queue; it still lands only on a passing gate.
The antoncore reconcile also accepts the admin role in `always` mode, the
fallback if a pull request mode jump is refused; `always` would also let an
admin push to dev without a pull request, so it stays out of the payload
until a jump needs it.
Dev requires the exact `Gate — Required` check from the Tests workflow and
allows squash merges with zero required approvals. Instead of requiring each
branch to be up to date, dev merges through a merge queue: the Tests workflow
runs on `merge_group`, so the full unit suite and test count floor grade the
merged tree before it lands. Mutation stays on the pull request head.
Pull requests enter the queue through the GraphQL `enqueuePullRequest`
mutation (`agentihooks swarm <slug> merge queue <url>` in a swarm) and land
when the queue run passes the gate. The installed gh predates merge queues, so
`gh pr merge --squash` calls the direct merge mutation, and the admin bypass
actor below lets it land past the queue and cancel the running group build.
The prod lockdown guard refuses `gh pr merge` into any branch with a merge
queue unless the operator ran `disable controls`.
The queue groups with `ALLGREEN`: every entry is built on dev plus the
entries ahead of it and merges only on its own passing run, so a red pull
request never lands. A failing entry leaves the queue and the entries behind
it rebuild without it. Merge groups run on the `anton-oss-queue` pool, which
serves only merge queue runs. The queue builds one entry at a time
and merges groups of up to five, waits at most one minute for a group to fill, and a
check that has not reported within 45 minutes fails the entry. Build
concurrency counts entries, not groups, and under `ALLGREEN` each entry
merges only on its own passing run. `max_entries_to_build` is one to prevent
simultaneous full unit suites competing for the capped queue runner pool;
`max_entries_to_merge` remains five.
An entry ejected only because a unit shard lost its runner is requeued once by
the Queue retry workflow; see
[Lost runner requeue](../../docs/reference/delivery-tiers.md#lost-runner-requeue).

The antoncore `Organization secrets and dev rulesets` workflow applies
`dev-history.json`, creating it when absent, then `dev-no-delete.json` from
this repository's dev and reads both back on every
antoncore dev push or dispatch, so a change here goes live on the next
antoncore dev push or a dispatch of that workflow. The reconcile requires the
required check to name its source, `integration_id` 15368 for GitHub Actions.
The operator applies the
other payloads. Agents cannot mutate remote rulesets. From a checkout
containing the reviewed payloads:

```bash
gh api \
  --method PUT \
  repos/The-Cloud-Clockwork/agentihooks/rulesets/15122747 \
  --input .github/rulesets/main-prod-lockdown.json
```

Verify the effective rules and bypass lists:

```bash
gh api repos/The-Cloud-Clockwork/agentihooks/rules/branches/dev
gh api repos/The-Cloud-Clockwork/agentihooks/rulesets/16090886 --jq .bypass_actors
gh api repos/The-Cloud-Clockwork/agentihooks/rulesets/25468405 --jq .bypass_actors
gh api repos/The-Cloud-Clockwork/agentihooks/rulesets/15122747 --jq .bypass_actors
gh api repos/The-Cloud-Clockwork/agentihooks/rules/branches/dev \
  --jq '.[] | select(.type == "pull_request") | .parameters.require_code_owner_review'
```

The dev `pull_request` rule carries `require_code_owner_review` false, as it
runs live: every path in `.github/CODEOWNERS` names the account that authors
the swarm's pull requests, so owner review waits on an approval nobody else
can give. Turning it on is a separate operator decision.
The dev response must include `pull_request`, `required_linear_history`,
`merge_queue` and `required_status_checks` requiring `Gate — Required` with
`strict_required_status_checks_policy` false; the dev bypass list holds the
two actors above and the main bypass list is empty.
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
