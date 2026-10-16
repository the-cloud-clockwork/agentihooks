---
title: Delivery tiers
parent: Reference
nav_order: 9
---

# Delivery tiers

AgentiHooks ships a package rule, `delivery-tiers`, that every installed profile loads. It tells agents where each CI check runs and what a red result means at each stage, so a slow check never holds a pull request and a red integration branch never stops the swarm.

## The three tiers

| Tier | Trigger | What runs | What it gates |
|---|---|---|---|
| **Pull request** | `pull_request` on each head push | lint, format, size and complexity, static analysis, coverage and mutation on changed lines, the tests the change touches | entry to the merge queue |
| **Queue** | `merge_group` | the unit suite, once per merge group | the merge into the integration branch, usually `dev` |
| **Promotion** | `push` to the integration branch, with `concurrency` preserving the running run and replacing only the pending run | whole module coverage and mutation, image builds and smoke tests, cluster and load tests, stage and shard time budget reports, then promotion | the release branch, usually `main`, and the release tag and images built from it |

Each tier has a wall time budget. A check that does not fit the pull request or queue budget moves to promotion.

How a green promotion run merges `dev` into `main` and creates the release is in [Promotion and releases](promotion.md).

## Rules

- **A red integration branch keeps merging; promotion does not advance.** A failed promotion run never closes the queue or holds other pull requests. The release branch stays on the last green promoted head until a later run passes.
- **Promotion owns the slow checks.** Nothing is dropped to make the earlier tiers fast; it runs at promotion instead, and a pull request is never held for a check only promotion runs.
- **A red promotion must produce one fixer task per failing check.** Each must be a `ci` task naming the job, the run and the commit, raised with a ledger alert and closed by the next green run. The fix is an ordinary pull request.
- **A lost runner is retried once.** A retry after runner loss is not a pass on a failed check.

## Coverage grading

The coverage ratchet fails a run when a line the base tree ran is no longer run by any test. Since the queue tier landed (pull request #3303), it runs on dev pushes, which form the promotion tier, and on dispatched Tests runs. The merge queue grades no coverage, so a queue entry never looks up or waits on the run of the entry ahead of it.

| Tests workflow event | `coverage-ratchet` | `coverage-baseline` | `queue-baseline` |
|---|---|---|---|
| `pull_request` | skipped | skipped | skipped |
| `merge_group` | skipped | skipped | skipped |
| `push` to `dev` | runs when every unit shard passed | runs when every unit shard passed | skipped |
| `workflow_dispatch` | runs against the dispatched base | skipped | skipped |

`Gate — Required` enforces the first two rows: on a pull request or a merge group every job outside that event's required set must be skipped, so a coverage job that ran there fails the gate. On a dev push a red ratchet fails the promotion run, and under the promotion rule the queue keeps merging.

### Where the baseline comes from

- **Publishing.** Every dev push whose unit shards all pass runs `coverage-baseline`, even when another gate of that run is red. It restores the history of the previous dev tree from the Actions cache, falling back to the newest saved `coverage-baseline-` entry when the exact tree has none, and computes `baseline.json` with `python -m tests.coverage_baseline --previous`. It saves the result under `coverage-baseline-<tree>` and uploads it as the `coverage-baseline` artifact, kept 90 days.
- **Grading on a dev push.** The ratchet takes the push's `before` commit as its base and resolves its baseline in this order:
  1. the cache entry for the base tree, `coverage-baseline-<tree>`;
  2. the newest `coverage-baseline-` cache entry, used only when the commit recorded in its `baseline.json` is an ancestor of the pushed head. This covers a base whose own push run was cancelled by a newer push before it published. The log line reads `Grading from the newest dev baseline at <commit>`, and the ratchet checks out that commit as its base;
  3. the live `coverage-baseline` artifact of a dev push run on the base commit whose unit shards and baseline publishing steps passed, even if another gate of that run was red, found through the API with the `tcc main ci` App token, when the cache has no usable entry.
- **Grading on a dispatch.** The `durations` job finds the measured dev push run of the dispatched base and republishes its baseline as `queue-coverage-baseline`; the ratchet grades against that.

The ratchet always runs `tests/coverage_ratchet.py` from the base checkout, never from the head under test, and uploads its findings as the `coverage-ratchet` artifact (`report.txt`, kept 7 days).

`queue-baseline` stays in the workflow with `if: false`, so it is always skipped, until `Gate — Required` and its clauses stop listing it.

### Troubleshooting

| Message in the `coverage-ratchet` log | Cause | Action |
|---|---|---|
| `No measured Tests run on base <sha> kept a live coverage-baseline artifact with successful measuring and publishing jobs.` | No cache entry fit and no dev push run of the base kept a live artifact with passed unit shards and publishing steps. | Rerun the failed jobs once the next dev push publishes a baseline, or dispatch the Tests workflow against a measured base. |
| `Base <sha> is outside dev history.` | The base commit is not on `dev`. | Dispatch the Tests workflow with a `base` on `dev`. |
| `The base carries no tests/coverage_ratchet.py, so nothing trusted can grade.` | The base predates the ratchet. | Dispatch the Tests workflow with a newer `base` input. |
| A coverage job ran in a merge group and `Gate — Required` failed with `Every gate required for this event must succeed and other gates must be skipped.` | A workflow change gave a coverage job a `merge_group` condition. | Restore the event conditions in the table above. |
