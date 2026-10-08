# Repository rulesets

These JSON files are update payloads for the existing repository rulesets,
using the [GitHub REST API](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset).
They contain writable settings only. Both rulesets enforce deletion and force
push protection, linear history and pull requests without bypass actors.
Dev requires the exact `Gate — Required` check from the Tests workflow and
allows squash merges with zero required approvals. Instead of requiring each
branch to be up to date, dev merges through a merge queue: the Tests workflow
runs on `merge_group`, so the gate grades the merged tree before it lands.
Main keeps its existing branch match and rules; only its bypass list changes.

The operator applies the payloads after the CI duration artifact and tag version
change has merged and passed its live proofs. Agents cannot mutate remote
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
```

The dev response must include `pull_request`, `required_linear_history`,
`merge_queue` and `required_status_checks` requiring `Gate — Required` with
`strict_required_status_checks_policy` false; both bypass lists must be empty.
The first pull request merged through the queue must show a Tests run on the
`merge_group` event whose `Gate — Required` passed. A controlled direct push attempt must be rejected by GitHub's pull
request rule. An unchanged ref or a dry run does not exercise that rule.
Record the rejection and the green and planted red CI run IDs on the task
and pull request before claiming the live controls are proven.
