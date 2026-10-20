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

## Checks a dev push skips

`lint` and `semgrep` pass on the pull request head, and `test-count` passes on the pull request head and again on the merged tree in the merge group. A push to `dev` carries a tree whose changes already passed them, so their job `if` in `.github/workflows/test.yml` carries `github.event_name != 'push'` and the promotion run skips them. `Gate — Required` checks each event's set:

- **Pull request:** `lint`, `semgrep` and `test-count` must succeed.
- **Merge group:** `test-count` must succeed; `lint` and `semgrep` must be skipped, like every job outside the queue set.
- **Dispatched run:** all three must succeed; a skip fails with `Every required gate must succeed.`
- **Push:** a skipped `lint`, `semgrep` or `test-count` passes.

| Job | `pull_request` | `merge_group` | `push` to `dev` |
|---|---|---|---|
| `lint` | runs | skipped | skipped |
| `semgrep` | runs | skipped | skipped |
| `test-count` (3.12) | runs | runs | skipped |
| `unit` and `shard-check` (3.12) | runs | runs | runs |
| `sonar` | runs | skipped | runs |

Every job runs Python 3.12 only. `sonar` stays on the push: the promotion tier runs the integration scan. The skip removed about 284 runner seconds from each dev push: `lint` 64 s, `semgrep` 42 s and `test-count` 88 s and 90 s for its two Python legs, as measured over the ten dev pushes before the change and recorded in pull request 3517. Delivered by pull request [3517](https://github.com/the-cloud-clockwork/agentihooks/pull/3517), after [3486](https://github.com/the-cloud-clockwork/agentihooks/pull/3486) taught the protected gate check (`gate-protected.yml`, `scripts.gate_protected`) to accept the `promotion` form of the gate step. The check runs the base branch's own `scripts.gate_protected`, whose `_aggregator_problems` accepts a head's `Gate — Required` steps only when they equal one of twelve renderings of `scripts.ci_reuse.gate_steps`: the keyword sets none, `selected`, `queue_tier`, `queue_tier` with `promotion`, `queue_tier` with `consolidated`, and all three of `queue_tier`, `promotion` and `consolidated`, each with `summary` false and true. `dev` now carries the form rendered with `queue_tier=True, promotion=True, consolidated=True, summary=True`. `tests/contracts/ci_tiers/test_workflow.py` pins the push job set.

Check which of these jobs the newest dev push run ran:

```bash
R=repos/the-cloud-clockwork/agentihooks
RUN=$(gh api "$R/actions/workflows/test.yml/runs?event=push&branch=dev&status=completed&per_page=1" --jq '.workflow_runs[0].id')
gh api "$R/actions/runs/$RUN/jobs?per_page=100" --jq '.jobs[]|"\(.name) | \(.conclusion)"' \
  | grep -E '^(lint|semgrep|test-count|sonar|Gate)'
```

Dev push run [39064628062](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39064628062) printed `lint`, `semgrep` and `test-count` as `skipped`, and `sonar` and `Gate — Required` as `success`.

| Symptom | Cause | Action |
|---|---|---|
| `Gate — Required` fails with `Every gate required for this event must succeed and other gates must be skipped.` on a pull request while `lint`, `semgrep` or `test-count` reads `skipped` | A workflow change dropped the job's pull request condition. | Restore the job `if` shown above. |
| `lint`, `semgrep` or `test-count` runs on a dev push | The `github.event_name != 'push'` clause was removed from its `if`. | Restore it; `tests/contracts/ci_tiers/test_workflow.py` fails on the change. |
| `gate-protected.yml` fails with `The head's Gate — Required changes its steps; the protected branch grades with its own.` | The head's gate step matches none of the twelve accepted renderings of `scripts.ci_reuse.gate_steps`. | Render the step from `scripts.ci_reuse.gate_steps` instead of editing it by hand; a new form lands first in its own pull request that teaches `scripts.gate_protected` to accept it. |

## Rules

- **A red integration branch keeps merging; promotion does not advance.** A failed promotion run never closes the queue or holds other pull requests. The release branch stays on the last green promoted head until a later run passes.
- **Promotion owns the slow checks.** Nothing is dropped to make the earlier tiers fast; it runs at promotion instead, and a pull request is never held for a check only promotion runs.
- **A red promotion must produce one fixer task per failing check.** Each must be a `ci` task naming the job, the run and the commit, raised with a ledger alert and closed by the next green run. The fix is an ordinary pull request.
- **A lost runner is retried once.** A retry after runner loss is not a pass on a failed check. In the merge queue the [Queue retry workflow](#lost-runner-requeue) does this for unit shards.

## Entering the merge queue

A pull request enters the `dev` merge queue at the head its pull request checks passed on. It is not brought up to date with `dev` first. The `dev` ruleset does not require an up to date branch (`strict_required_status_checks_policy` is `false` in `.github/rulesets/dev-no-delete.json`, see the [rulesets README](https://github.com/the-cloud-clockwork/agentihooks/blob/dev/.github/rulesets/README.md)), and the merge group runs the unit suite on that head merged with the newest `dev` and the entries ahead of it. Merging `dev` into a green branch only creates a new head, which needs a second full pull request run before it can be queued. In agentihooks this relies on an active merge queue on `dev` and on the Tests workflow running on `merge_group`; the command takes only the swarm slug, the agent identity (`--as` or `AGENTIHOOKS_AGENT_NAME`) and the pull request URL.

In a swarm, an engineer queues its own pull request once its checks pass and both reviews close:

```bash
agentihooks swarm <slug> merge queue https://github.com/<owner>/<repo>/pull/<number>
```

The command:

1. Reads the pull request. It refuses a pull request whose base is not `dev`, and one that is not the calling worker's recorded task pull request.
2. Reads the required checks of the pull request's own repository: its `dev` branch rules (`repos/<owner>/<repo>/rules/branches/dev`) and `dev` branch protection, each check by name and, where the rule names one, by app. It refuses unless every required check has reported on the current head and none failed. No workflow file name is assumed, so a repository whose required check comes from a workflow other than Tests is graded the same way.
3. Enqueues that head with `expectedHeadOid`. GitHub refuses the enqueue if the branch moved after step 1.
4. Prints the pull request's state, head and queue entry:

```json
{"url": "https://github.com/<owner>/<repo>/pull/<number>", "state": "OPEN", "head": "<head sha>", "queued": true, "entry": {"id": "<entry id>", "position": 1, "state": "QUEUED"}}
```

A pull request already in the queue returns its state without another check or enqueue. For one that is not queued, it reads no `dev` comparison, commit list, job or log, never calls GitHub's update branch, and never commits to the branch, whatever changed on `dev` since the checks ran, CI and grading files included. The seat then waits with `agentihooks swarm <slug> wait --on merge <url>`. A pull request released with `agentihooks swarm <slug> ready` is queued the same way by the tick's merge pass once its checks pass.

Merge `dev` into the branch only on a real conflict or when GitHub refuses the enqueue. That commit is a new head, so the command refuses it until the required checks on that head pass.

A repository with no merge queue on `dev` gets a squash merge from the same command instead, only for a ready pull request with green checks and a review decision of none or approved; see the merge command row in the [swarm commands](../pillars/swarm.md).

| Message | Cause | Action |
|---|---|---|
| `required checks on the current pull request head must pass before queueing` | A required check of the pull request's repository has not reported on the current head, is still running, or failed. A failed check is named after `; failed:`. | Wait on checks with `agentihooks swarm <slug> wait --on checks <url>`, or fix the red check, then queue again. |
| `swarm merge queue operations require a pull request into dev` | The pull request base is not `dev`. | Retarget the pull request to `dev`. |
| `gh: Not Found (HTTP 404)` | The caller's `gh` cannot read the `dev` branch rules of the repository named in the URL. | Check `gh auth status`; the account must read that repository's branch rules and pull requests. |
| `GitHub URL does not identify a pull request` | The URL names no pull request the caller can read. | Check the owner, repository and number in the URL. |
| `final integration must target this task's recorded pull request` | The URL is not recorded on the calling worker's task. | Record it with `agentihooks swarm <slug> pr <url>`. |
| A GitHub GraphQL error from the enqueue | GitHub refused the entry, for example because the head moved after the read or the pull request conflicts with `dev`. | Read `agentihooks swarm <slug> merge state <url>`, fix the cause, and queue once the new head is green. |

`tests/swarm/test_merge_queue.py` pins this behaviour: a green head is queued as is whatever `dev` changed, and a red head is refused without reading `dev`. Delivered by pull request [3476](https://github.com/the-cloud-clockwork/agentihooks/pull/3476). Its Tests run on the merged head passed: [38840793791](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38840793791). The red and green proofs it cites are Proofs workflow dispatches on branch `ci-323133-0814`: [38745333203](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38745333203) failed on the old path, which still updated the branch from `dev`, and [38746154923](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38746154923) passed on the new one.

`test_queue_adds_another_repositorys_pull_request_to_its_own_merge_queue` in the same file pins the cross repository case below: given a `dev` merge queue and the kernel's required check in its fixture, a pull request in another repository (the test runs antoncore and agentibrain-kernel) joins that repository's queue, and every read addresses that repository. Delivered by pull request [4187](https://github.com/the-cloud-clockwork/agentihooks/pull/4187); its red Proofs run [39058116611](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39058116611) failed on the old Tests workflow lookup with `gh: Not Found (HTTP 404)`, and [39058118542](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39058118542) passed.

### Pull requests in other repositories

The command works on any repository the caller's `gh` can reach, not only agentihooks. Every read and the enqueue address the repository named in the URL, and the merge queue it joins is that repository's own `dev` queue. A task that spans repositories records each pull request with `agentihooks swarm <slug> pr <url>` and queues each one the same way, in dependency order:

```bash
agentihooks swarm <slug> merge queue https://github.com/the-cloud-clockwork/agentibrain-kernel/pull/<number>
```

For agentibrain-kernel, whose `dev` rules require `Gate — Required` reported by GitHub Actions (`gh api repos/the-cloud-clockwork/agentibrain-kernel/rules/branches/dev` lists it with `integration_id` 15368), the command grades that check, not a Tests run, and enqueues into the kernel's `dev` merge queue. A repository with no merge queue on `dev`, such as antoncore, takes the squash merge described above. The checks wait accepts a pull request or an Actions run in another repository the same way: `agentihooks swarm <slug> wait --on checks https://github.com/<owner>/<repo>/actions/runs/<id>` reads only that repository's run and, when it is red, its jobs.

## Lost runner requeue

A merge group whose unit shard loses its runner fails `Gate — Required`, and GitHub removes the pull request from the queue. The Queue retry workflow, `.github/workflows/queue-retry.yml`, puts it back once, so a lost runner costs neither a queue slot nor an agent round trip. A real test failure is never requeued.

### When it runs

Queue retry runs on `workflow_run` when a Tests run completes. Its job starts only when that run:

- ran on the `merge_group` event,
- ended `failure` or `cancelled`, and
- is the first attempt of the run (`run_attempt` 1).

GitHub runs `workflow_run` workflows from the default branch, `main`. A change to `queue-retry.yml` or `scripts/ci_queue_retry.py` merged on `dev` takes effect only after it is [promoted](promotion.md) to `main`. Until then the queue keeps the behaviour of the last promoted release.

### How a failed run is classified

`python -m scripts.ci_queue_retry --repo <owner>/<repo> --run-id <id>` reads the run and its latest failed jobs, then decides:

1. Every failed job must be a `unit` shard or a job that depends on the `unit` jobs, directly or through other jobs, in `.github/workflows/test.yml` (for example `shard-check`, `refresh-durations` and `Gate — Required`). A failure in any other job, such as `lint` or `test-count`, refuses the requeue. So does a run with more than 100 jobs, since the jobs beyond the first page are counted as unclassified failures.
2. At least one `unit` shard must have failed. A `cancelled` shard counts as failed only when it carries a runner loss signature; other cancelled jobs are ignored.
3. Every failed `unit` shard must carry a runner loss signature in its check annotations or in the `##[error]` lines of its job log:
   - `The runner has received a shutdown signal`
   - `lost communication with the server`
   - `The job was not acquired by Runner`

   One red shard beside a lost one refuses the requeue.
4. The pull request number comes from the merge group branch, `gh-readonly-queue/<base>/pr-<number>-<sha>`.
5. While the pull request is still in the queue, the classifier polls every 30 seconds, up to 30 minutes, for GitHub to drop it. The job's 35 minute timeout covers that wait.
6. The pull request must be open and outside the queue. If it left the queue since its head commit, the newest removal must be for failed checks, and there must be at most one failed checks removal. A head that already failed in the queue twice, or that last left the queue for another reason, is not requeued. Pushing a new head starts the count again.

The step prints one JSON line with the decision and writes `requeue`, `pull`, and, on a requeue, the pull request `node` id and `head` commit to the step outputs:

```json
{"run": 1234567890, "pull": 4321, "requeue": true, "reason": "runner lost on unit (3.12, 4); first failed checks removal of this head"}
```

### Enforcement

The repository variable `QUEUE_RETRY_ENFORCE` controls what happens when the decision is `requeue: true`:

| `QUEUE_RETRY_ENFORCE` | Behaviour |
|---|---|
| unset, or any value other than `false` | The workflow mints a token for the TCC Main CI GitHub App (`TCC_CI_CLIENT_ID`, `TCC_CI_APP_PRIVATE_KEY`, with contents and pull request write) and calls the GraphQL `enqueuePullRequest` mutation with the classified head as `expectedHeadOid`, which adds the pull request back to the queue. A push after classification makes the head differ and GitHub refuses the requeue. The `Requeue the pull request once` step shows the result. |
| `false` | Log only. The workflow writes the notice `Log only, runner loss would requeue pull request <number> once` and leaves the pull request out of the queue. |

Enforcement is on by default. To return to log only, without a code change:

```bash
gh variable set QUEUE_RETRY_ENFORCE --body false --repo the-cloud-clockwork/agentihooks
```

To turn enforcement back on, delete the variable:

```bash
gh variable delete QUEUE_RETRY_ENFORCE --repo the-cloud-clockwork/agentihooks
```

The variable is read at run time, so it applies to the next failed merge group run. A pull request requeued this way merges only on its own passing run, like any other queue entry.

### Checking a requeue

```bash
gh run list --workflow queue-retry.yml --limit 5
gh run view <run id> --log | grep -E '"requeue"|Log only'
```

| What you see | Cause | Action |
|---|---|---|
| The Queue retry job is skipped | The Tests run was not a merge group, did not fail, or was a rerun attempt. | None. Only first attempt merge group failures are classified. |
| `"requeue": false` with `failed outside the unit shards: <jobs>` | A job other than the unit shards and their dependents failed. | Fix that job; the pull request needs a new queue entry once its checks pass. |
| `"requeue": false` with `failed without a runner loss signature: <shards>` | A shard failed on a test, or its loss left none of the three signatures. | Read the shard log. Fix a real failure; queue again by hand for an unrecognised runner loss. |
| `"requeue": false` with `head already left the queue 2 times on failed checks` | The same head was already requeued once and failed again. | Treat it as a real failure until the shard logs show otherwise. |
| `"requeue": false` with `pull request is not open outside the queue` | The pull request was closed or merged, already queued again, or still in the queue after the 30 minute wait. | None, unless it is open and outside the queue; then queue it by hand. |
| `"requeue": false` with `no unit shard failed` | The run failed or was cancelled with no unit shard carrying a failure or a runner loss signature. | Read the failed job; queue again by hand once it is fixed. |
| `Classify the failed jobs` fails with `gh: HTTP 404` on `actions/jobs/<id>/logs` | GitHub kept no log for a shard, which can happen when its runner was lost. No decision is printed and nothing is requeued. | Queue the pull request by hand. |
| The `Log only` notice | `QUEUE_RETRY_ENFORCE` is `false`. | Queue the pull request by hand, or delete the variable to turn enforcement on. |
| A change to the retry rules has no effect | It is merged on `dev` but not promoted to `main`. | Wait for the next green promotion; see [Promotion and releases](promotion.md). |

`tests/test_ci_queue_retry.py` pins the classification and the once per head guard against recorded shard logs in `tests/fixtures/queue_retry/`. Enforcement by default was delivered by pull request [3511](https://github.com/the-cloud-clockwork/agentihooks/pull/3511) under issue [3508](https://github.com/the-cloud-clockwork/agentihooks/issues/3508). Later pull requests made the classifier wait for the queue to drop the pull request ([4166](https://github.com/the-cloud-clockwork/agentihooks/pull/4166), [4206](https://github.com/the-cloud-clockwork/agentihooks/pull/4206)), requeue through `enqueuePullRequest` ([4241](https://github.com/the-cloud-clockwork/agentihooks/pull/4241)) and count a cancelled shard that lost its runner ([4271](https://github.com/the-cloud-clockwork/agentihooks/pull/4271)).

### Stalled runs

A job whose runner pod or node vanished can stay `in_progress` with no failure, so its Tests run never completes and Queue retry never sees it. The Lost runner workflow, `.github/workflows/lost-runner.yml`, is scheduled every ten minutes (GitHub can start scheduled runs late) on a hosted runner, queues on a shared hosted lane, and runs `python -m scripts.ci_lost_runner --repo <owner>/<repo>` with the TCC Main CI app token. It examines every unfinished Tests run (in progress, queued, pending, waiting or requested), whatever its event. A run is stalled when:

- a job started at least ten minutes ago and runs on a self-hosted runner that is absent from or offline in the organization runner list,
- no other job is running on a live runner,
- every other unfinished job waits on a lost job, and
- the same holds on the second read.

A stalled run is cancelled, then force cancelled if a plain cancel does not end it. Its failed jobs are rerun once only when it is a first attempt and no job failed or timed out on its own; a run that failed on its own is never rerun, and a second loss ends cancelled. Every suspect run is confirmed on a second read at least two minutes after the first, and one tick waits only once. Each examined run prints one JSON line with its action, `none`, `cancel`, `rerun` or `error`, and the reason. A refused API call reports `error` for that run, the other runs are still examined, and the job fails. Like Queue retry, a scheduled workflow runs from `main`, so a change takes effect after promotion.

GitHub can list a dead runner online for over a minute, and the scheduled workflow starts at most every ten minutes, so the swarm tick also runs the watchdog with `--cluster` on the tick host, where `kubectl` can read the cluster; CI runners cannot. The tick step `lost_runner.sweep` starts it at most once a minute per repository, only where `kubectl` exists and the repository has the Tests workflow, and a file lock under the AgentiHooks home keeps one watchdog acting at a time; a later tick finding it held exits and the next free minute looks again. The watchdog runs isolated (`python -I`), so it imports the installed AgentiHooks package, and a change to it reaches the tick when the installed checkout pulls `dev`. In cluster mode a runner is lost when its pod ended (phase Failed or Succeeded, or its `runner` container terminated), sits on a node NotReady for at least sixty seconds or on a node the cluster no longer lists, is in phase Unknown on a Ready node, or is missing from a scale set the cluster lists. There is no job age grace and the second read comes thirty seconds after the first. The only reads are `kubectl get autoscalingrunnersets --all-namespaces`, `kubectl get pods --all-namespaces -l actions.github.com/scale-set-name` and `kubectl get nodes`, so the tick host's kubeconfig needs list rights on those three. A refused cluster read reports `error` and never counts as a loss, and a runner whose scale set the cluster does not list is never counted lost. Its output goes to `lost-runner.log` under the AgentiHooks home.

## Coverage grading

The coverage ratchet fails a run when a line the base tree ran is no longer run by any test. Since the queue tier landed (pull request #3303), it runs on dev pushes, which form the promotion tier, and on dispatched Tests runs. The merge queue grades no coverage, so a queue entry never looks up or waits on the run of the entry ahead of it.

| Tests workflow event | `coverage-ratchet` | `coverage-baseline` |
|---|---|---|
| `pull_request` | skipped | skipped |
| `merge_group` | skipped | skipped |
| `push` to `dev` | runs when every unit shard passed | runs when every unit shard and `coverage-ratchet` passed |
| `workflow_dispatch` | runs against the dispatched base | skipped |

`Gate — Required` enforces the first two rows: on a pull request or a merge group every job outside that event's required set must be skipped, so a coverage job that ran there fails the gate. On a dev push a red ratchet fails the promotion run, and under the promotion rule the queue keeps merging.

### Where the baseline comes from

- **Publishing.** Every dev push whose unit shards and `coverage-ratchet` pass runs `coverage-baseline`, even when another gate of that run is red. It restores the history of the previous dev tree from the Actions cache, falling back to the newest saved `coverage-baseline-` entry when the exact tree has none, and computes `baseline.json` with `python -m tests.coverage_baseline --previous`. It saves the result under `coverage-baseline-<tree>` and uploads it as the `coverage-baseline` artifact, kept 90 days.
- **Grading on a dev push.** The ratchet takes the push's `before` commit as its base and resolves its baseline in this order:
  1. the cache entry for the base tree, `coverage-baseline-<tree>`;
  2. the newest `coverage-baseline-` cache entry, used only when the commit recorded in its `baseline.json` is an ancestor of the pushed head. This covers a base whose own push run was cancelled by a newer push before it published. The log line reads `Grading from the newest dev baseline at <commit>`, and the ratchet checks out that commit as its base;
  3. the live `coverage-baseline` artifact of a dev push run on the base commit whose unit shards and baseline publishing steps passed, even if another gate of that run was red, found through the API with the `tcc main ci` App token, when the cache has no usable entry.
- **Grading on a dispatch.** The `durations` job finds the measured dev push run of the dispatched base and republishes its baseline as `queue-coverage-baseline`; the ratchet grades against that.

The ratchet always runs `tests/coverage_ratchet.py` from the base checkout, never from the head under test, and uploads its findings as the `coverage-ratchet` artifact (`report.txt`, kept 7 days).

The disabled `queue-baseline` job is gone: pull request [3517](https://github.com/the-cloud-clockwork/agentihooks/pull/3517) removed it from the workflow, from the `needs` of `coverage-ratchet`, `stage-budget` and `Gate — Required`, from the `ci_budget` stage budgets and from `.github/runner-classes.json`. Two references stay for runs made before the removal: the ratchet's baseline lookup still reads the `queue-baseline` steps of an older non-push run, and the `Gate — Required` step that `scripts.ci_reuse.gate_steps` renders still accepts a skipped `queue-baseline` outside a merge group.

### Troubleshooting

| Message in the `coverage-ratchet` log | Cause | Action |
|---|---|---|
| `No measured Tests run on base <sha> kept a live coverage-baseline artifact with successful measuring and publishing jobs.` | No cache entry fit and no dev push run of the base kept a live artifact with passed unit shards and publishing steps. | Rerun the failed jobs once the next dev push publishes a baseline, or dispatch the Tests workflow against a measured base. |
| `Base <sha> is outside dev history.` | The base commit is not on `dev`. | Dispatch the Tests workflow with a `base` on `dev`. |
| `The base carries no tests/coverage_ratchet.py, so nothing trusted can grade.` | The base predates the ratchet. | Dispatch the Tests workflow with a newer `base` input. |
| `<n> lines the base ran are no longer run by any head test; see the coverage-ratchet report.` | A merged change removed or rerouted the only test reaching those lines, listed by file and line in `report.txt`, so promotion is red while the queue keeps merging. | Add or restore a test that reaches each line through a normal pull request; never lower the ratchet or exclude the line. |
| A coverage job ran in a merge group and `Gate — Required` failed with `Every gate required for this event must succeed and other gates must be skipped.` | A workflow change gave a coverage job a `merge_group` condition. | Restore the event conditions in the table above. |
