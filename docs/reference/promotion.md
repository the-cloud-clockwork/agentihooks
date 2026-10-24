---
title: Promotion and releases
parent: Reference
nav_order: 10
---

# Promotion and releases

A green push run of the Tests workflow on `dev` promotes the tree it graded into `main` as a squash commit and creates a patch release from it. No agent and no operator opens or merges the `dev` to `main` pull request: CI does it with the TCC Main CI GitHub App. This is the promotion tier described in [Delivery tiers](delivery-tiers.md).

## When promotion runs

The `promote` job in `.github/workflows/test.yml` is the last job of a `dev` push run. It calls the reusable `.github/workflows/promote.yml` only when all of these hold:

| Condition | Source |
|---|---|
| The event is `push` and the ref is `refs/heads/dev` | job `if` in `test.yml`, repeated in `promote.yml` and `promote.cjs` |
| `Gate — Required` succeeded | `needs.gate-required.result == 'success'` |
| Hierarchy CPU proof succeeded | `needs.hierarchy-cpu.result == 'success'`; see [Hierarchy CPU proof at promotion](delivery-tiers.md#hierarchy-cpu-proof-at-promotion) |
| Sonar produced an analysis | `needs.sonar.outputs.analysis != ''` |
| No dependency failed or was cancelled | `gate-required`, `refresh-durations`, `coverage-baseline`, `flake-budget`, `sonar` and `hierarchy-cpu`; `!contains(needs.*.result, 'failure')` and `'cancelled'` |
| The run was not cancelled | `!cancelled()` |

Pull request and merge queue runs never promote. A dispatched Tests run never promotes.

Every `dev` push run shares the concurrency group `promotion-dev` with `cancel-in-progress` off and a single pending slot. A running run always finishes and, when green, promotes the revision it graded even after `dev` has moved on. A newer push only replaces the run still waiting in the pending slot, so a burst of merges grades the running head and the newest head, not every head in between. Runs in the group never overlap, so promotions land in push order.

## What a promotion does

The job runs on `ubuntu-latest` in the `release` environment, which deploys only from `dev`, with a ten minute timeout. It holds the `release-version` concurrency group without cancelling, the same group the manual `release.yml` uses, so two version computations never overlap.

1. **Mint an App token.** `actions/create-github-app-token` uses the `TCC_CI_CLIENT_ID` and `TCC_CI_APP_PRIVATE_KEY` secrets, scoped to this repository with metadata read and contents, pull requests and workflows write.
2. **Compare trees.** When `main` already carries the graded tree, no pull request is made and the job goes straight to release. When the `main` commit message names an earlier graded revision (`Graded dev revision: <sha>`), the graded revision must descend from it on `dev`. A revision that is that revision, an ancestor of it or diverged from it ends superseded: the step sets the `superseded` output to the revision `main` names, writes a `Promotion superseded` summary, and merges, tags and releases nothing, so a rerun of an old run never rolls `main` back. The check needs that line on the current `main` commit, which every promotion made by this job writes; a `main` commit without it skips the check.
3. **Snapshot the graded tree on `main`.** The pull request never uses `dev` as its head, because `dev` keeps moving, and never the graded commit itself: each squash promotion puts a commit on `main` that `dev` does not contain, so a later `dev` commit would conflict with it on every file changed again. The head is a snapshot branch `promotion/<graded sha>` holding one commit whose tree is the graded tree and whose only parent is the current `main`. The job always reads the snapshot from that branch; it creates the commit and branch only when the branch is missing and no promotion pull request from it is open. The job fails with `Promotion snapshot is not the graded tree on main` when the snapshot commit has another tree or any parents other than exactly the current `main`.
4. **Open or reuse the pull request.** An open pull request from the snapshot branch into `main` is reused; otherwise one is created titled `Promote green dev to main`, whose body names the graded revision and links the Tests run. The job fails when the pull request head is not the snapshot commit, its head repository is not this repository, or its base is not `main`.
5. **Wait for mergeability.** Every five seconds, up to 48 times (four minutes), it reads the pull request. It fails when the pull request head moved away from the snapshot commit or GitHub reports conflicts, and proceeds once GitHub reports it mergeable and `clean`. An `unstable` head is also admitted when the current run's required grade on the graded `dev` commit succeeded and its exact running promotion job is the only unfinished check. The job is identified by its run, job id and check URL; all checks and effective commit statuses are read with pagination. Every other check must succeed or be legitimately skipped, and the latest status for every context must succeed.
6. **Squash merge.** It reads `main` again first. When `main` moved during the wait, the step 2 check runs on the new head: a newer promotion closes this run's pull request, deletes its snapshot branch and ends the run superseded, and any other move fails it with `Promotion snapshot is stale, main moved during the wait`, because the snapshot's parent is no longer `main`. It then merges with `merge_method: squash`, `sha` set to the snapshot commit and a commit message naming the graded revision and the Tests run, so GitHub refuses the merge if the head changed. `main` keeps linear history, as the `main-prod-lockdown` ruleset requires.
7. **Verify the tree.** The new `main` commit must carry the same tree as the graded `dev` commit; otherwise the job fails. The squash creates a new commit id, so tree equality is the identity check, not commit equality.
8. **Delete the snapshot.** The `promotion/<graded sha>` branch is deleted; a branch GitHub already removed is accepted.
9. **Compute the version.** It checks out the promoted `main` commit and runs `scripts/ci_release.sh version` with `BUMP=patch` and `REUSE_TREE=true`. When a stable `vX.Y.Z` tag already points at the same tree, that version is reused and no new tag is made. Otherwise the next patch version follows the latest stable tag.
10. **Tag and verify.** For a new version, `ci_release.sh tag` tags the promoted `main` commit `vX.Y.Z`, and `ci_release.sh verify` builds the wheel and checks its metadata version equals `X.Y.Z` (setuptools-scm derives the package version from the tag). The tag is then pushed.
11. **Release.** Unless the release already exists, `ci_release.sh notes` assembles every `changelog.d/` fragment added since the previous stable tag into one section per Keep a Changelog heading, and `gh release create vX.Y.Z --title vX.Y.Z --notes-file <assembled notes> --generate-notes --latest --verify-tag` creates the GitHub release with those entries ahead of the generated pull request list.
12. **Close abandoned attempts.** The last step runs after any promotion outcome except cancellation, with the same App token. Every open pull request into `main` that the App opened from `promotion/<graded sha>` in this repository, whose graded revision is an ancestor of the revision this run graded, gets a comment naming the superseding revision and run, is closed, and loses its snapshot branch while that branch still points at the pull request head. The current attempt, newer or diverged attempts, other authors, forks and other branches stay open. The step stops examining pull requests after three minutes so the ten minute job timeout holds, and the summary names how many it left for the next run. The job summary lists each outcome; any failure fails the step after the other pull requests are handled, and the release steps before it have already run.

The job summary records the graded `dev` commit, the promoted `main` commit and the tree. The `promote` step outputs `graded`, `main` and, when it merged, `pull-request`, or `graded` and `superseded` when it ended superseded; the release steps read `main`.

Promotion never publishes to PyPI and never dispatches another workflow. PyPI publication stays the operator's `publish-pypi.yml` run from `main`, with the `pypi` environment reviewer.

## Merge step time

The `Squash the graded tree snapshot into main` step runs `promote.cjs`, so it covers steps 2 to 8 of the list above: snapshot, pull request, the mergeability wait, the squash, the tree check and the branch delete. Its time is the step 5 wait plus the API calls of the other steps; the command below reports only the step total.

Pushing a `promotion/**` branch starts no Mutation preflight run: `mutation-preflight.yml` lists `promotion/**` in its push `branches-ignore`, beside `dev`, `main`, `gh-readonly-queue/**`, `wip/**` and `diffcheck/**`. Before [#3886](https://github.com/the-cloud-clockwork/agentihooks/pull/3886) the push ran a mutation preflight on the promotion head, a tree whose changes had already been mutation graded on their pull requests, and the merge waited for its `mutation` check. Other workflows still run on that push; on the snapshot of v2.17.2 they were `grade` (Branch workflow lanes) and two `measure` checks (Startup stages, Worker resources).

The step can still wait on the graded `dev` commit. When the promotion pull request reports `unstable`, step 5 admits it only while this run's `promote / promote` job is the one check still running, this run's `Gate — Required` succeeded, every other check on the graded commit has completed with success or skipped, and the latest commit status of every context is success. A slow non-gate workflow on that commit, such as the Worker hive pod `kind` job, holds the merge until it finishes or the four minute limit fails the step.

Measured on promotion runs of the Tests workflow, check times from the commits API and merge times from the pulls API:

| Run | Mutation wait on the promotion head | Step seconds | Last check before the merge | Release |
|---|---|---|---|---|
| [38950625810](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38950625810) | yes | 172 | `mutation` on the promotion head, 19:05:12Z; merged 19:05:17Z | v2.17.1 |
| [39043823157](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39043823157) | no | 241 | Worker hive pod `publish` (run [39043823047](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39043823047)) on the graded commit, 15:21:01Z; merged 15:21:09Z | v2.17.2 |
| [39044572864](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39044572864) | no | 15 | `Gate — Required` on the graded commit, 15:28:25Z; merged 15:28:47Z | v2.17.3 |

Only one promotion before the change ran this step, so there is one baseline. In all three runs the `Tag and verify the promoted package` and `Create the release with generated notes` steps succeeded, and the promoted `main` commit carries the graded tree. The 241 s run used nearly the whole four minute wait: the step started at 15:17:11Z and `publish` completed 230 s later. A graded commit whose slowest non-gate workflow outlasts the wait fails the promotion with nothing merged.

Measure the step on a promotion run:

```bash
R=The-Cloud-Clockwork/agentihooks
gh run list -R "$R" -w Tests -e push -b dev -L 5                                   # pick a run id
RUN=<Tests push run id on dev>
gh api --paginate "repos/$R/actions/runs/$RUN/jobs?per_page=100" --jq '
  .jobs[] | select(.name == "promote / promote") | .steps[]
  | select((.name | startswith("Squash the graded")) or (.name | test("Tag and verify|Create the release")))
  | {name, conclusion, seconds: ((.completed_at | fromdate) - (.started_at | fromdate))}'
```

Promotions before [#3668](https://github.com/the-cloud-clockwork/agentihooks/pull/3668) name the step `Squash the graded dev head into main`; the `startswith` match reads both names. List what the step waited on from the check times of the graded commit:

```bash
R=The-Cloud-Clockwork/agentihooks
GRADED=<head sha of the run>
gh api --paginate "repos/$R/commits/$GRADED/check-runs?per_page=100" \
  --jq '.check_runs[] | [.completed_at, .name] | @tsv' | sort | tail -5
```

Checks completing after the promotion pull request's `merged_at`, such as `promote / promote` itself, played no part; the last line before `merged_at` is the check that held the merge. Check the tree identity with the commands under [Checking a promotion](#checking-a-promotion).

## Reruns and repeats

A rerun after a partial failure is safe:

- `main` already on the graded tree skips the snapshot, pull request and merge.
- An open promotion pull request from the snapshot branch is reused, and a snapshot branch left on the graded tree over the current `main` is reused.
- A stable tag on that tree is reused (`existing=true`), so the tag step is skipped.
- A release that already exists is not recreated.

A rerun after another promotion moved `main` ends superseded before any snapshot or write; a promotion pull request it opened earlier stays open until a newer promotion run closes it as abandoned.

## When nothing is promoted

| Situation | Outcome |
|---|---|
| `Gate — Required` or any promotion dependency is red or cancelled | The `promote` job is skipped. `main` stays on the last promoted head; `dev` keeps merging. |
| A newer push to `dev` arrived while a run was pending | The pending run is replaced and never runs; the running run still finishes and promotes its own revision when green. |
| The snapshot commit has another tree or parents other than exactly the current `main`, or the pull request conflicts or its head differs from the snapshot commit, or it is not mergeable and clean within four minutes | The job fails and nothing is merged or tagged. |
| The graded revision does not descend from the revision `main` names, at the start or after the mergeability wait | The run ends superseded with a green job; nothing is merged or tagged. |
| `main` moved during the wait without naming a newer graded revision | The job fails before merging and nothing is tagged. |
| GitHub refuses the merge | The job fails before tagging. |
| The promoted tree differs | The job fails before tagging; `main` has already advanced to the merged commit and the snapshot branch is kept for inspection. A rerun of that revision is refused, because `main` already names it. |

A red promotion produces one fixer task per failing check under the [delivery tiers rule](delivery-tiers.md#rules); the fix lands through an ordinary pull request into `dev`, and the next green push run promotes it. When several swarms watch the repository, one of them owns that intake; see [promotion fixer intake](../pillars/swarm.md#promotion-fixer-intake).

## Controller intake without git

The swarm controller checks promotion failures during its tick. Its repository
lookup works in the controller image even when that image has no `git`
executable. This keeps promotion fixer intake running; it does not change the
checks required to release. See [promotion fixer intake](../pillars/swarm.md#promotion-fixer-intake)
for owner election, duplicate suppression and handover.

Repository identity is selected automatically:

| Lookup result | Identity used for intake ownership |
|---|---|
| `git rev-parse --path-format=absolute --git-common-dir` succeeds | The resolved git common directory; a primary checkout and its worktrees share ownership |
| Starting `git` raises `FileNotFoundError` | The resolved checkout path |
| `git` exits with a nonzero status, including outside a repository | The resolved checkout path |

The owner record hashes that identity. No flag enables the fallback and no git
installation is required for this lookup. `AGENTIHOOKS_SWARM_KEY_PREFIX` changes
the Redis key prefix, as described in [choosing the owner](../pillars/swarm.md#choosing-the-owner);
it does not choose the repository identity. For example, two linked swarms
using `/workspace/agentihooks` still elect one intake owner without git. Two
different checkout paths do not share that fallback identity, even when they
are worktrees of the same repository. Keep linked watchers on the same resolved
checkout path when git is unavailable.

### Diagnose a missing git controller crash

The failure repaired by [pull request 4380](https://github.com/the-cloud-clockwork/agentihooks/pull/4380)
was a controller crash loop ending in `FileNotFoundError` for `git`, reached
through `promotion_watch.refresh`, promotion owner intake and
`promotion_owner.repository`. Before that repair, the missing executable escaped
the repository lookup instead of using the checkout path.

Inspect the failed CI job and compare it with a later dev push after the repair:

```bash
R=The-Cloud-Clockwork/agentihooks
BEFORE=39092038586
AFTER=39098125691
gh run view "$BEFORE" -R "$R" --log-failed
gh api "repos/$R/actions/runs/$AFTER/jobs?filter=latest" --paginate \
  --jq '.jobs[] | select(.name == "helm-kind / kind") | {name, conclusion, html_url}'
```

These are the delivered repair's evidence runs; substitute the failing run and
a later run containing your repair when diagnosing a new failure. A skipped or
cancelled job does not prove recovery. If the same stack trace recurs, check
whether the controller image includes the repair. Deliver any further fix
through code and CI; changing a running container does not repair its image.

The original dev push [39092038586](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39092038586)
failed with the missing git error. The repair's [green regression run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39094909940)
passed 23 tests, including resolved path fallback and a linked watched swarm and
Doctor retaining one fixer without git on `PATH`. Its [mutation preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39094879605)
killed all 24 mutants. After merge, [helm-kind / kind in dev push 39098125691](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39098125691/job/117589554915)
succeeded. That job proves the missing git recovery; it does not establish that
every check in that push passed or that a release was promoted.

## Manual releases

`release.yml` remains for manual and dry run releases:

```bash
gh workflow run release.yml --ref dev -f bump=patch          # patch, minor or major
gh workflow run release.yml --ref dev -f bump=minor -f dry_run=true
```

A real run must start from `dev`; a dry run tags and builds only inside the CI checkout. It uses the same `scripts/ci_release.sh` stages and the same `release-version` lock as promotion.

## Checking a promotion

```bash
R=The-Cloud-Clockwork/agentihooks
GRADED=<head sha of the green dev push run>
gh run list -R "$R" -w Tests -e push -b dev -L 5                                   # dev push runs
git fetch origin main dev                                                           # refresh both heads
git rev-parse 'origin/main^{tree}' "$GRADED^{tree}"                                 # equal after promoting the graded commit $GRADED
gh release list -R "$R" -L 3                                                        # newest release
gh pr list -R "$R" --base main --state all -S 'Promote green dev to main' -L 3     # promotion pull requests
```

Immediately after a green push run promotes, `main` carries the graded `dev` tree, a patch tag points at that tree (a new one on the promoted `main` commit, or the existing tag reused) and a GitHub release with generated notes exists for it. A later promotion moves `main` past it.

## Configuration

| Setting | Where | Purpose |
|---|---|---|
| `TCC_CI_CLIENT_ID`, `TCC_CI_APP_PRIVATE_KEY` | repository secrets, passed by `test.yml` | Mint the App token that creates and deletes the snapshot branch, opens and merges the promotion pull request, pushes the tag and creates the release |
| `release` environment | `.github/environments/release.json` | Deployment only from `dev`; the promotion job and `release.yml` both use it |
| `version-tags.json` ruleset | `.github/rulesets/` | A `v*` tag cannot be moved, force pushed or deleted |
| `main-prod-lockdown` ruleset | `.github/rulesets/` | `main` needs a pull request and linear history, which the squash merge satisfies |
| `promote` budget | `scripts/ci_budget` | Matches the job's ten minute timeout |

The environment and ruleset payloads and how to apply them are in `.github/rulesets/README.md`.
