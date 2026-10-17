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

## Entering the merge queue

A pull request enters the `dev` merge queue at the head its pull request checks passed on. It is not brought up to date with `dev` first. The `dev` ruleset does not require an up to date branch (`strict_required_status_checks_policy` is `false` in `.github/rulesets/dev-no-delete.json`, see the [rulesets README](https://github.com/the-cloud-clockwork/agentihooks/blob/dev/.github/rulesets/README.md)), and the merge group runs the unit suite on that head merged with the newest `dev` and the entries ahead of it. Merging `dev` into a green branch only creates a new head, which needs a second full pull request run before it can be queued. This relies on an active merge queue on `dev` and on the Tests workflow running on `merge_group`; the command takes only the swarm slug, the agent identity (`--as` or `AGENTIHOOKS_AGENT_NAME`) and the pull request URL.

In a swarm, an engineer queues its own pull request once its checks pass and both reviews close:

```bash
agentihooks swarm <slug> merge queue https://github.com/<owner>/<repo>/pull/<number>
```

The command:

1. Reads the pull request. It refuses a pull request whose base is not `dev`, and one that is not the calling worker's recorded task pull request.
2. Reads the newest `pull_request` run of the Tests workflow on the current head. It refuses unless that run passed on that head.
3. Enqueues that head with `expectedHeadOid`. GitHub refuses the enqueue if the branch moved after step 1.
4. Prints the pull request's state, head and queue entry:

```json
{"url": "https://github.com/<owner>/<repo>/pull/<number>", "state": "OPEN", "head": "<head sha>", "queued": true, "entry": {"id": "<entry id>", "position": 1, "state": "QUEUED"}}
```

A pull request already in the queue returns its state without another check or enqueue. For one that is not queued, it reads no `dev` comparison, commit list, job or log, never calls GitHub's update branch, and never commits to the branch, whatever changed on `dev` since the checks ran, CI and grading files included. The seat then waits with `agentihooks swarm <slug> wait --on merge <url>`. A pull request released with `agentihooks swarm <slug> ready` is queued the same way by the tick's merge pass once its checks pass.

Merge `dev` into the branch only on a real conflict or when GitHub refuses the enqueue. That commit is a new head, so the command refuses it until the Tests run on that head passes.

A repository with no merge queue on `dev` gets a squash merge from the same command instead, only for a ready pull request with green checks and a review decision of none or approved; see the merge command row in the [swarm commands](../pillars/swarm.md).

| Message | Cause | Action |
|---|---|---|
| `the current pull request head must pass Tests before queueing` | No `pull_request` Tests run on the current head yet, it is still running, or it failed. | Wait on checks with `agentihooks swarm <slug> wait --on checks <url>`, or fix the red check, then queue again. |
| `swarm merge queue operations require a pull request into dev` | The pull request base is not `dev`. | Retarget the pull request to `dev`. |
| `final integration must target this task's recorded pull request` | The URL is not recorded on the calling worker's task. | Record it with `agentihooks swarm <slug> pr <url>`. |
| A GitHub GraphQL error from the enqueue | GitHub refused the entry, for example because the head moved after the read or the pull request conflicts with `dev`. | Read `agentihooks swarm <slug> merge state <url>`, fix the cause, and queue once the new head is green. |

`tests/swarm/test_merge_queue.py` pins this behaviour: a green head is queued as is whatever `dev` changed, and a red head is refused without reading `dev`. Delivered by pull request [3476](https://github.com/the-cloud-clockwork/agentihooks/pull/3476). Its Tests run on the merged head passed: [38840793791](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38840793791). The red and green proofs it cites are Proofs workflow dispatches on branch `ci-323133-0814`: [38745333203](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38745333203) failed on the old path, which still updated the branch from `dev`, and [38746154923](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38746154923) passed on the new one.

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
