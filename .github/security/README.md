# Operator GitHub access controls

`access.json` records the settings applied by `scripts/github_access.sh`.
The branch payload covers every agentihooks branch except
`refs/heads/gh-readonly-queue/**`, which GitHub creates for its merge queue.
Dev and main retain their independent pull request rules without bypass actors.

GitHub repository rulesets cannot name a user as a bypass actor. Repository
administrators are allowed instead; the audit requires the operator to be the
only organization member and the only collaborator with write or admin access.
The only app bypass actor is Agenticore App. The read-only TCC Main CI app has
no bypass. Those are also the only two permitted organization installations.

Agents prepare and review the change but cannot apply remote enforcement.
The master opens an operator pane in the reviewed checkout. The operator uses
his own authenticated `gh` or a token referenced through `GH_TOKEN`, then runs:

```bash
bash scripts/github_access.sh apply
```

The command updates outside-contributor approval and workflow-token permissions
on agentihooks and agentibrain-kernel, disables public repository access to the
Default runner group, and updates the existing agentihooks branch restriction.
After applying settings, it opens the installation settings pages of any
unapproved apps. The operator uninstalls Claude, ChatGPT Codex Connector and AWS
Connector for GitHub there, then presses Enter in the same pane to read every
setting back. GitHub's uninstall REST endpoint requires the third-party app's
JWT, which the operator's token cannot provide. Preserve the complete output
on the task. Read-back stays red while any unapproved installation remains.
Run the same read-only verification independently with:

```bash
bash scripts/github_access.sh check
```

The check also verifies organization defaults, membership, repository writers,
teams, deploy keys and installed apps. API failures and missing settings fail the command.
Successful reads print each checked setting and end with
`GitHub access controls verified`. Drift prints `DRIFT` and exits nonzero.

The hosted audit uses the existing organization secret `ORG_GITHUB_TOKEN`.
Its token must read repository administration, organization members/settings,
installed applications and organization self-hosted runner groups. A classic
operator token needs `repo` and `admin:org`; a fine-grained equivalent requires
the corresponding read permissions. The job never applies settings and never
runs on pull request code. It runs on dev pushes and manual dispatch immediately;
GitHub activates its weekly schedule only when the workflow reaches the default
branch through the next release. Dispatch it through the existing focused proof
workflow, which owns the hosted lane:

```bash
gh workflow run proofs.yml \
  --ref dev \
  -f proof=github-access \
  -f base=origin/dev
```

The merge queue exception must be proved by a subsequent successful merge group
run after operator application. If the access restriction blocks the queue,
the operator disables only that additional ruleset while its payload is fixed;
the dev and main protections stay active. Public access to Default stays disabled.
