# Three tier CI templates

Copy pull-request.yml, merge-queue.yml and promotion.yml into .github/workflows and delivery.yaml into the repository root. These assets ship inside the installed profiles package, under package/templates/ci. They are examples to adapt before enabling required checks, not a ready made test suite.

The integration branch is dev and the release branch is main. Change the branch filters and the promotion concurrency group together if your repository uses other names. Enable the merge queue for the integration branch. Require Delivery required for the integration branch; both entry workflows emit that same check name, so it is present on pull request heads and merge groups. Do not require Promotion checks on the queue.

| Tier | Event | Commands |
|---|---|---|
| Pull request | pull_request | lint, format, size, static analysis, changed line ratchets, touched tests |
| Queue | merge_group | unit suite once per group |
| Promotion | push to dev | module ratchets, images, smoke, load and budget reports |

Each workflow reads its command list from delivery.yaml with PyYAML and runs each command in Bash with errexit, nounset and pipefail. Add your executable ci scripts first. Each script owns installation of the tools and dependencies its checks need. Commands must exit nonzero on failure; budget-report records measurements without failing solely for a budget overrun. Keep the queue list limited to the unit suite and never cancel a merge group merely because a pull request head changed. Pull request checkout uses the head revision, while queue checkout uses the synthetic merge group revision.

CI_RUNNER_LABEL is an optional repository variable; unset means ubuntu-latest. A single runner label is accepted. Use GitHub hosted runners for public or untrusted pull requests. Setting this variable to a self hosted label also places pull request code on that runner: isolate disposable workers and do not expose credentials or internal networks. The workflows contain no actor allowlist.

Create a promotion environment restricted to the integration branch. Configure PROMOTION_APP_ID as a repository variable and PROMOTION_APP_PRIVATE_KEY as an environment secret. Install the GitHub App on this repository with contents and pull requests write permissions and any checks permissions your release policy requires. Ordinary checks use only a read only token and checkout never persists credentials. The App token is exposed only to the promote command after promotion checks pass.

Implement ci/promote to accept the graded commit as its first argument. It must confirm that revision still equals the integration head, open or update the integration to release pull request, wait for the release rules to pass, merge with rebase, and verify the release tree equals the graded tree. Then create a version tag and release and publish version and release image tags from that tree. It must fail rather than promote a newer unchecked head; concurrency cancellation alone cannot reverse a completed remote write. Idempotence must cover partial releases and cancelled runs. This repository specific release logic is deliberately supplied by the adopter, not hidden in the example.

Connect a promotion failure watcher to your task tracker before enabling delivery: create one ci fixer task per failing check with its run and commit, deduplicate while open, alert, and close on the next green run. These examples stop promotion on red; they do not implement a tracker adapter. A red promotion leaves the queue open. On infrastructure runner loss only, classify the failure and retry the failed job once; never retry a failing assertion until it passes. Supply that classifier in your infrastructure rather than adding unconditional retries to the checks.

The package repository validates all three workflow files with actionlint in CI, including an invalid workflow control. After adoption, run actionlint on the copied workflows in your own CI too.

The promote job reads current main under the release lock before invoking the adopter command. Every release commit must preserve a line `Graded dev revision: <full forty character dev commit>` identifying the revision it carries, including after rebase or squash. Only a strict descendant of that revision may promote; equal, older and divergent heads are refused. A markerless initial main can bootstrap when it is an ancestor of the graded head. An existing markerless rewritten release needs authoritative revision metadata established by the adopter's release migration before enabling this workflow; tree equality cannot identify which dev revision was released. Missing provenance or a failed comparison stops promotion before the adopter command runs.
