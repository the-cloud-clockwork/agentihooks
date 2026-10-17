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
| No dependency failed or was cancelled | `gate-required`, `refresh-durations`, `coverage-baseline` and `flake-budget`; `!contains(needs.*.result, 'failure')` and `'cancelled'` |
| The run was not cancelled | `!cancelled()` |

Pull request and merge queue runs never promote. A dispatched Tests run never promotes.

Every `dev` push run shares the concurrency group `promotion-dev` with `cancel-in-progress` off and a single pending slot. A running run always finishes and, when green, promotes the revision it graded even after `dev` has moved on. A newer push only replaces the run still waiting in the pending slot, so a burst of merges grades the running head and the newest head, not every head in between. Runs in the group never overlap, so promotions land in push order.

## What a promotion does

The job runs on `ubuntu-latest` in the `release` environment, which deploys only from `dev`, with a ten minute timeout. It holds the `release-version` concurrency group without cancelling, the same group the manual `release.yml` uses, so two version computations never overlap.

1. **Mint an App token.** `actions/create-github-app-token` uses the `TCC_CI_CLIENT_ID` and `TCC_CI_APP_PRIVATE_KEY` secrets, scoped to this repository with metadata read and contents, pull requests and workflows write.
2. **Close superseded promotions.** Every open pull request into `main` whose head is a `promotion/<sha>` branch of this repository, whose commit is an ancestor of the graded revision, has its snapshot branch deleted and is then closed; a branch GitHub already removed is accepted, and a failed deletion fails the job with the pull request still open for the next run. This runs before the tree comparison and the merge, so a promotion that later fails leaves no superseded promotion pull request open. The current snapshot, a diverged or newer snapshot, a fork head and any other branch are left alone.
3. **Compare trees.** When `main` already carries the graded tree, no pull request is made and the job goes straight to release.
4. **Snapshot the graded revision.** The pull request never uses `dev` as its head, because `dev` keeps moving. The head is a snapshot branch `promotion/<graded sha>`. When no open pull request from that branch exists, the job creates the branch at the graded commit, or reuses it when it already points there, and fails with `Promotion snapshot differs from the graded revision` when it points elsewhere.
5. **Open or reuse the pull request.** An open pull request from the snapshot branch into `main` is reused; otherwise one is created titled `Promote green dev to main`, whose body names the graded revision and links the Tests run. The job fails when the pull request head is not the graded commit, its head repository is not this repository, or its base is not `main`.
6. **Wait for mergeability.** Every five seconds, up to 48 times (four minutes), it reads the pull request. It fails when the pull request head moved away from the graded commit or GitHub reports conflicts, and proceeds once GitHub reports it mergeable and `clean`. An `unstable` head is also admitted when the current run's required grade succeeded and its exact running promotion job is the only unfinished check. The job is identified by its run, job id and check URL; all checks and effective commit statuses are read with pagination. Every other check must succeed or be legitimately skipped, and the latest status for every context must succeed.
7. **Squash merge.** It merges with `merge_method: squash` and `sha` set to the graded commit, so GitHub refuses the merge if the head changed. `main` keeps linear history, as the `main-prod-lockdown` ruleset requires.
8. **Verify the tree.** The new `main` commit must carry the same tree as the graded `dev` commit; otherwise the job fails. The squash creates a new commit id, so tree equality is the identity check, not commit equality.
9. **Delete the snapshot.** The `promotion/<graded sha>` branch is deleted; a branch GitHub already removed is accepted.
10. **Compute the version.** It checks out the promoted `main` commit and runs `scripts/ci_release.sh version` with `BUMP=patch` and `REUSE_TREE=true`. When a stable `vX.Y.Z` tag already points at the same tree, that version is reused and no new tag is made. Otherwise the next patch version follows the latest stable tag.
11. **Tag and verify.** For a new version, `ci_release.sh tag` tags the promoted `main` commit `vX.Y.Z`, and `ci_release.sh verify` builds the wheel and checks its metadata version equals `X.Y.Z` (setuptools-scm derives the package version from the tag). The tag is then pushed.
12. **Release.** Unless the release already exists, `ci_release.sh notes` assembles every `changelog.d/` fragment added since the previous stable tag into one section per Keep a Changelog heading, and `gh release create vX.Y.Z --title vX.Y.Z --notes-file <assembled notes> --generate-notes --latest --verify-tag` creates the GitHub release with those entries ahead of the generated pull request list.

The job summary records the graded `dev` commit, the promoted `main` commit and the tree. The `promote` step outputs `graded`, `main` and, when it merged, `pull-request`; the release steps read `main`.

Promotion never publishes to PyPI and never dispatches another workflow. PyPI publication stays the operator's `publish-pypi.yml` run from `main`, with the `pypi` environment reviewer.

## Reruns and repeats

A rerun after a partial failure is safe:

- `main` already on the graded tree skips the snapshot, pull request and merge.
- An open promotion pull request from the snapshot branch is reused, and a snapshot branch left at the graded commit is reused.
- A stable tag on that tree is reused (`existing=true`), so the tag step is skipped.
- A release that already exists is not recreated.

## When nothing is promoted

| Situation | Outcome |
|---|---|
| `Gate — Required` or any promotion dependency is red or cancelled | The `promote` job is skipped. `main` stays on the last promoted head; `dev` keeps merging. |
| A newer push to `dev` arrived while a run was pending | The pending run is replaced and never runs; the running run still finishes and promotes its own revision when green. |
| No open promotion pull request exists and the snapshot branch points at another commit, or the pull request conflicts or its head differs from the graded commit, or it is not mergeable and clean within four minutes | The job fails and nothing is merged or tagged. |
| GitHub refuses the merge | The job fails before tagging. |
| The promoted tree differs | The job fails before tagging; `main` has already advanced to the merged commit and the snapshot branch is kept for inspection. |

A red promotion produces one fixer task per failing check under the [delivery tiers rule](delivery-tiers.md#rules); the fix lands through an ordinary pull request into `dev`, and the next green push run promotes it.

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
