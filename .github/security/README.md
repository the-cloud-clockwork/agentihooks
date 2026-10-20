# Operator GitHub access controls

`access.json` records the settings applied by `scripts/github_access.sh`.
The audit requires the operator to be the only organization member and the
only collaborator with write or admin access. Agenticore App and the read-only
TCC Main CI app are the only permitted organization installations.
Dev and main retain their independent pull request rules. Main has no bypass
actors; dev lets the admin role and Agenticore App bypass on pull requests
so they can jump the merge queue, as `../rulesets/README.md` records.

Agents prepare and review the change but cannot apply remote enforcement.
The master opens an operator pane in the reviewed checkout. The operator uses
his own authenticated `gh` or a token referenced through `GH_TOKEN`, then runs:

```bash
bash scripts/github_access.sh apply
```

The command updates outside-contributor approval and workflow-token permissions
on agentihooks and agentibrain-kernel, disables public repository access to the
Default runner group.
After applying settings, it opens the installation settings pages of any
unapproved apps. The operator uninstalls Claude, ChatGPT Codex Connector and AWS
Connector for GitHub there, then presses Enter in the same pane to read every
setting back. GitHub's uninstall REST endpoint requires the third-party app's
JWT, which the operator's token cannot provide. Preserve the complete output
on the task. Read-back stays red while any unapproved installation remains.
Strict approval for all outside contributors on the organization and every
public repository is reconciled by Antoncore's dev-only org automation. The
inventory includes archived repositories and uses every GitHub API page.
The readback verifies the organization, every discovered public repository,
and the Anton open source runner group's selected repositories. Failed or
empty public discovery fails the audit. The group keeps unrestricted workflows
so trusted pull request jobs can run; outside contributors require approval.
Run the same read-only verification independently with:

```bash
bash scripts/github_access.sh check
```

The check also verifies organization defaults, membership, repository writers,
teams, deploy keys and installed apps. API failures and missing settings fail the command.
Successful reads print each checked setting and end with
`GitHub access controls verified`. Drift prints `DRIFT` and exits nonzero.

The hosted audit mints an installation token from TCC Main CI using the existing
`TCC_CI_CLIENT_ID` and `TCC_CI_APP_PRIVATE_KEY` secrets, as the reuse jobs do.
The app's client ID identifies the same app as `TCC_CI_APP_ID`; the token action
prefers client ID over its deprecated app ID input. The app must be installed on
agentihooks and agentibrain-kernel with repository Administration read, Actions
read and Contents read, plus organization Administration read, Members read and
Self-hosted runners read. The token requests only those read permissions and
all repositories in the installation. Organization Administration allows the audit to list
installed applications and inspect organization defaults. Personal tokens are
not used; `ORG_GITHUB_TOKEN` remains private. The job never applies settings and
never runs on pull request code. It runs on dev pushes and manual dispatch immediately;
GitHub activates its weekly schedule only when the workflow reaches the default
branch through the next release. Dispatch it through the existing focused proof
workflow, which owns the hosted lane:

```bash
gh workflow run proofs.yml \
  --ref dev \
  -f proof=github-access \
  -f base=origin/dev
```

The proposed all-branch creation/update ruleset is a separate follow-up.
The master reported that its merge queue exclusion still blocked queue creation
and stalled every merge. This apply command leaves that rule unchanged; a
queue-compatible restriction needs an independent live proof before activation.
