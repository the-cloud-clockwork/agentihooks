---
title: Promotion and releases
parent: Reference
nav_order: 10
---

# Promotion and releases

A green push run of the Tests workflow on `dev` promotes that exact `dev` head into `main` and creates a patch release from it. No agent and no operator opens or merges the `dev` to `main` pull request: CI does it with the TCC Main CI GitHub App. This is the promotion tier described in [Delivery tiers](delivery-tiers.md).

## When promotion runs

The `promote` job in `.github/workflows/test.yml` is the last job of a `dev` push run. It calls the reusable `.github/workflows/promote.yml` only when all of these hold:

| Condition | Source |
|---|---|
| The event is `push` and the ref is `refs/heads/dev` | job `if` in `test.yml`, repeated in `promote.yml` and `promote.cjs` |
| `Gate — Required` succeeded | `needs.gate-required.result == 'success'` |
| No dependency failed or was cancelled | `gate-required`, `refresh-durations`, `coverage-baseline` and `flake-budget`; `!contains(needs.*.result, 'failure')` and `'cancelled'` |
| The run was not cancelled | `!cancelled()` |
| The graded commit is still the `dev` head | `promote.cjs` reads `heads/dev` and returns without writing when it moved |

Pull request and merge queue runs never promote. A dispatched Tests run never promotes.

Every `dev` push run shares the concurrency group `promotion-dev` with `cancel-in-progress` on, so a newer push cancels the run grading an older head. Only the newest head can reach promotion, and a cancelled run promotes nothing.

## What a promotion does

The job runs on `ubuntu-latest` in the `release` environment, which deploys only from `dev`, with a ten minute timeout. It holds the `release-version` concurrency group without cancelling, the same group the manual `release.yml` uses, so two version computations never overlap.

1. **Mint an App token.** `actions/create-github-app-token` uses the `TCC_CI_CLIENT_ID` and `TCC_CI_APP_PRIVATE_KEY` secrets, scoped to this repository with metadata read and contents, pull requests and workflows write.
2. **Compare trees.** When `main` already carries the graded tree, no pull request is made and the job goes straight to release.
3. **Open or reuse the pull request.** An open `dev` to `main` pull request is reused; otherwise one is created titled `Promote green dev to main`, whose body names the graded revision and links the Tests run. The job fails when the pull request head is not the graded commit, its head repository is not this repository, or its base is not `main`.
4. **Wait for mergeability.** Every five seconds, up to 48 times (four minutes), it reads the pull request. It returns without merging when `dev` moved, fails when the head moved or GitHub reports conflicts, and proceeds once GitHub reports it mergeable and `clean`.
5. **Rebase merge.** It checks the `dev` head once more, then merges with `merge_method: rebase` and `sha` set to the graded commit, so GitHub refuses the merge if the head changed. `main` keeps linear history, as the `main-prod-lockdown` ruleset requires.
6. **Verify the tree.** The new `main` commit must carry the same tree as the graded `dev` commit; otherwise the job fails. The rebase rewrites commit ids, so tree equality is the identity check, not commit equality.
7. **Compute the version.** It checks out the promoted `main` commit and runs `scripts/ci_release.sh version` with `BUMP=patch` and `REUSE_TREE=true`. When a stable `vX.Y.Z` tag already points at the same tree, that version is reused and no new tag is made. Otherwise the next patch version follows the latest stable tag.
8. **Tag and verify.** For a new version, `ci_release.sh tag` tags the promoted `main` commit `vX.Y.Z`, and `ci_release.sh verify` builds the wheel and checks its metadata version equals the tag (setuptools-scm derives the package version from the tag). The tag is then pushed.
9. **Release.** Unless the release already exists, `gh release create vX.Y.Z --generate-notes --latest --verify-tag` creates the GitHub release.

The job summary records the graded `dev` commit, the promoted `main` commit and the tree. Its outputs are `graded`, `main` and, when it merged, `pull-request`.

Promotion never publishes to PyPI and never dispatches another workflow. PyPI publication stays the operator's `publish-pypi.yml` run from `main`, with the `pypi` environment reviewer.

## Reruns and repeats

A rerun after a partial failure is safe:

- `main` already on the graded tree skips the pull request and merge.
- A stable tag on that tree is reused (`existing=true`), so the tag step is skipped.
- A release that already exists is not recreated.

## When nothing is promoted

| Situation | Outcome |
|---|---|
| `Gate — Required` or any promotion dependency is red or cancelled | The `promote` job is skipped. `main` stays on the last promoted head; `dev` keeps merging. |
| A newer push to `dev` arrived | The older run is cancelled, or `promote.cjs` returns without writing when it sees the new head. |
| The pull request conflicts, its head moved, or it is not mergeable and clean within four minutes | The job fails and nothing is merged or tagged. |
| GitHub refuses the merge, or the promoted tree differs | The job fails before tagging. |

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
gh run list -R "$R" -w Tests -e push -b dev -L 5                                   # dev push runs
git ls-remote origin refs/heads/main refs/heads/dev                                 # the two heads
git rev-parse 'origin/main^{tree}' 'origin/dev^{tree}'                              # equal after promotion
gh release list -R "$R" -L 3                                                        # newest release
gh pr list -R "$R" --base main --head dev --state all -L 3                          # promotion pull requests
```

After a green push run, `main` carries the graded `dev` tree, a new patch tag points at the promoted `main` commit and a GitHub release with generated notes exists for it.

## Configuration

| Setting | Where | Purpose |
|---|---|---|
| `TCC_CI_CLIENT_ID`, `TCC_CI_APP_PRIVATE_KEY` | repository secrets, passed by `test.yml` | Mint the App token that opens and merges the promotion pull request, pushes the tag and creates the release |
| `release` environment | `.github/environments/release.json` | Deployment only from `dev`; the promotion job and `release.yml` both use it |
| `version-tags.json` ruleset | `.github/rulesets/` | A `v*` tag cannot be moved, force pushed or deleted |
| `main-prod-lockdown` ruleset | `.github/rulesets/` | `main` needs a pull request and linear history, which the rebase merge satisfies |
| `promote` budget | `scripts/ci_budget` | Matches the job's ten minute timeout |

The environment and ruleset payloads and how to apply them are in `.github/rulesets/README.md`.
