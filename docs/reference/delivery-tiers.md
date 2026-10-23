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

## Suite speed checks on pull requests

On a pull request, the `select` job in `.github/workflows/test.yml` runs `scripts.ci_select` to choose the tests the change reaches. It prints one of three scopes:

| Scope | When | What runs |
|---|---|---|
| `full` | The change touches a harness file or a `conftest.py`, the selected tests would need more than three legs, or no test was selected and the change is not documentation only | `split`, `unit` and `shard-check`, the whole suite |
| `selected` | One or more tests were selected and fit in three legs | `unit-selected`, one job per leg, on the files listed in `files_1`, `files_2` and `files_3` |
| `none` | Every changed file is documentation (`docs/` or a `.md` file) | no unit tests |

The merge queue always runs the whole suite. `tests/test_suite_speed.py` holds two whole suite checks that read every test module mentioning `fakeredis`:

- `test_fakeredis_test_modules_import_without_loading_redis`: importing those modules loads neither `redis` nor `fakeredis`. Import `fakeredis` inside the test or fixture that uses it, not at module level. On failure the assertion names the first module that loaded each package.
- `test_fakeredis_test_modules_run_on_every_worker`: no such module carries an `xdist_group` mark, so the fakeredis modules spread across every pytest worker.

Before pull request 4220 these checks ran only in full suite runs, so a pull request with a `selected` scope could pass its own tier and then be ejected from the queue. On 17 October that happened to two Doctor pull requests. Each ejection cost a merge group run.

### When the selection adds them

`scripts.ci_select` adds `tests/test_suite_speed.py` to a `selected` run when all three hold:

1. At least one other test was selected. A change that selects nothing still runs the full suite or, for documentation only, no tests.
2. `tests/test_suite_speed.py` exists in the pull request checkout.
3. Either the change adds, edits, renames or deletes a file under `tests/` named `test_*.py`, or the text of any selected test file contains `fakeredis`.

The changed file list comes from `git diff --name-only --no-renames`, so a renamed test module counts as a deleted path and an added path. The `fakeredis` text match covers a source change in `hooks/` or `scripts/` whose selected tests use fakeredis, even when no test module changed. Nothing else in the selection changes: the checks share a leg with the other selected files and do not change the scope.

| Change | Suite speed checks |
|---|---|
| Adds or edits `tests/swarm/test_plain.py` | selected |
| Deletes `tests/test_gone.py` and edits `tests/test_other.py` | selected |
| Edits `hooks/sample.py`, imported by a test module that uses fakeredis | selected |
| Edits `hooks/sample.py`, whose selected tests never mention fakeredis | not selected |
| Edits only a non-Python file such as `scripts/static/ledger.js` | not selected, unless a selected test mentions fakeredis |
| Changes `pyproject.toml` or a `conftest.py` | full suite, which already runs them |

There is no setting to configure. The `select` step runs the selector from the base branch copy checked out under `$RUNNER_TEMP/grader`, so a pull request that changes `scripts/ci_select.py` is still graded by the rule already on `dev`. The rule applied to pull requests whose runs started after pull request 4220 merged.

### Writing a fakeredis test module that passes

```python
import pytest

pytestmark = pytest.mark.unit


def test_store_round_trip():
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    ...
```

Keep the `fakeredis` import and any import that loads `redis` inside the test or a fixture. Leave the `xdist_group` mark out of `pytestmark`.

### Checking a run

Read the `select` job output of the pull request's Tests run:

```bash
R=repos/the-cloud-clockwork/agentihooks
RUN=<Tests run id>
JOB=$(gh api "$R/actions/runs/$RUN/jobs?per_page=100" --jq '.jobs[]|select(.name=="select")|.id')
gh api "$R/actions/jobs/$JOB/logs" | grep -E 'Z (scope|count|files_[0-9])='
```

Pull request [5785](https://github.com/the-cloud-clockwork/agentihooks/pull/5785) edited `scripts/doctor/read.py` and `tests/doctor/test_read.py`. Its [Tests run 39746367123](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39746367123) printed:

```text
scope=selected
files_1=tests/doctor/test_empty_seats.py tests/doctor/test_read.py tests/test_suite_speed.py
```

The checks then fail inside `unit-selected` on the pull request instead of in the merge group.

Delivered by pull request [4220](https://github.com/the-cloud-clockwork/agentihooks/pull/4220). Its [red run 39064033422](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39064033422) planted an unregistered fakeredis module, which the old selector left out. `tests/test_ci_select.py` holds the cases from the table above.

### Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `unit-selected` fails `test_fakeredis_test_modules_import_without_loading_redis` | A fakeredis test module imports `fakeredis` or `redis`, directly or through a module it imports, at module level. The assertion message names the module. | Move the import into the test or fixture. |
| `unit-selected` fails `test_fakeredis_test_modules_run_on_every_worker` | A fakeredis test module sets `pytest.mark.xdist_group` in `pytestmark`. | Remove the mark. |
| `unit-selected` fails with `Import probe exited` | A test module that mentions fakeredis raises on import. The failure prints the probe's stderr, whose last `Importing` line names the module. | Fix the import error in that module. |
| The queue ejects a pull request on a suite speed check its own run did not select | The change selected nothing, or no selected test mentions fakeredis, while a merge from `dev` or another queue entry brought in the failing module. | Read the merge group run's failed job, fix the named module in its own pull request, then requeue. |

## Rules

- **A red integration branch keeps merging; promotion does not advance.** A failed promotion run never closes the queue or holds other pull requests. The release branch stays on the last green promoted head until a later run passes.
- **Promotion owns the slow checks.** Nothing is dropped to make the earlier tiers fast; it runs at promotion instead, and a pull request is never held for a check only promotion runs.
- **A red promotion must produce one fixer task per failing check.** Each must be a `ci` task naming the job, the run and the commit, raised with a ledger alert and closed by the next green run. The fix is an ordinary pull request. Failed jobs that lost their runner have no code cause and share one [runner outage task](../pillars/swarm.md#runner-outage-runs) instead.
- **A lost runner is retried once.** A retry after runner loss is not a pass on a failed check. In the merge queue a unit shard whose runner was lost is [rerun inside the same merge group run](#lost-unit-shard-rerun).

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
| `staged compatibility: <reason>` | The pull request body declares a staged prerequisite and candidate pair whose proof is incomplete, stale or unreadable on the exact heads. | Fix the reason named in [Staged compatibility before admission](../pillars/swarm.md#staged-compatibility-before-admission), then queue again. |
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

## Lost unit shard rerun

A merge group unit shard that loses its runner is rerun once inside the same Tests run, so the pull request stays in the queue and merges on the rerun's green.

1. `queue-fail-fast` reads the log of each failed `unit (<python>, <shard>)` job once on the App token. Only an error line `Process completed with exit code <1 to 127>` with no runner loss signature proves a real failure and cancels the run at once; a timed out shard also cancels it. A lost runner can leave no log, or only an annotation the App token cannot read, so any other shard failure waits for the classifier. Every other failed job, the classifier and the rerun included, cancels the run at once. A run whose group was rebuilt is cancelled too, after the watcher writes a [queue rebuild record](#the-watcher-record).
2. `unit-lost` starts when the `unit` matrix ends in failure or cancelled. On the App token it lists this attempt's jobs and runs `python -m scripts.ci_unit_rerun --jobs <listing> --repo <owner>/<repo> --plan <queue plan>`, which judges each failed shard's log by the watcher's rule. A timed out shard, a shard whose log proves a real failure, or a lost leg missing from the queue shard plan makes `unit-lost` fail and the queue drops the entry.
3. `unit-rerun` runs the `unit` job's own steps once for each lost leg, from the same queue shard plan, and overwrites that leg's durations, flake and coverage artifacts. `shard-check` waits for it, so completeness is graded on the rerun's results.
4. `Gate — Required` accepts a merge group only when `unit` passed with both rerun jobs skipped, or `unit` failed or was cancelled and both `unit-lost` and `unit-rerun` passed. A rerun that fails for any reason, a second runner loss included, fails the gate and the queue drops the entry. Queue retry never requeues it: a run whose `unit rerun` job failed is refused with `failed again on its in-run rerun`.

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
| `"requeue": false` with `failed again on its in-run rerun: <jobs>` | A shard lost its runner, was rerun inside the run, and the rerun failed. | Read the rerun log; queue again by hand once the cause is fixed. |
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

## Queue rebuild records

GitHub rebuilds a merge group whenever an entry ahead of it changes: an enqueue or priority insertion ahead, a dequeue, a requeue, a removal ahead or a push to the base. The rebuilt group gets a new head commit, and the Tests run still building the old head is cancelled. On 16 October a priority insertion rebuilt three group chains and cancelled 195.550 runner minutes, and no record named the action that caused it ([issue 4282](https://github.com/the-cloud-clockwork/agentihooks/issues/4282)). Two records now keep that link:

- the **queue journal**, written on the machine running the swarm tooling for every queue mutation that tooling performs, and
- the **watcher record**, written in CI by the stale group watcher before it cancels a run whose group is no longer current.

Neither record changes what the queue does. Both were delivered by [pull request 4355](https://github.com/the-cloud-clockwork/agentihooks/pull/4355); [pull request 5706](https://github.com/the-cloud-clockwork/agentihooks/pull/5706) added the removal ahead cause.

### The queue journal

Every record is one JSON line appended to `merge-queue-events.jsonl` in the AgentiHooks home (`$AGENTIHOOKS_HOME`, else `~/.agentihooks`). Nothing needs to be enabled. A write that fails prints `merge queue record not written: <error>` on stderr, and the queue operation still completes.

| Action | Written by | Queue read before and after |
|---|---|---|
| `queue` | `agentihooks swarm <slug> merge queue <url>`, the `ready` merge pass and the requeue sweep `python -m scripts.ci_requeue` | yes |
| `dequeue` | `agentihooks swarm <slug> merge dequeue <url>` and the `ready` merge pass | yes |
| `priority` | `agentihooks swarm <slug> merge priority <url>` when it reorders the queue, at once or when the tick replays a deferred request | yes |
| `priority-deferred` | A priority request that waits because the head group's merge group run has not completed | no |
| `priority-lift` | A priority request with `--lift`, which skips that wait | no |
| `priority-applied` | The tick replaying a deferred priority that succeeded | no |
| `priority-failed` | The tick replaying a deferred priority that failed | no |
| `priority-dropped` | The tick dropping a deferred priority whose pull request closed, left `dev` or became a draft | no |
| `priority-requeue` | The tick requeuing an entry an interrupted priority left out | no |
| `priority-requeue-skipped` | The tick leaving such an entry out because it became a draft, its head moved or it conflicts with `dev` | no |

`merge state` writes nothing. A `queue` for a pull request already in the queue writes nothing, and neither does a `queue` into a repository with no `dev` merge queue, which squash merges instead.

Each record carries:

| Field | Meaning |
|---|---|
| `id`, `at` | A random hex identifier and the UTC write time |
| `action` | One of the actions above |
| `token_actor` | The GitHub login of the token that read the queue, which is the actor GitHub records for the mutation; `null` when no queue read succeeded |
| `agent` | `AGENTIHOOKS_AGENT_NAME` of the calling session; `null` outside a swarm session |
| `pull`, `expected_head` | The pull request URL, always set, and the head it was queued or dequeued at; `expected_head` is empty for `priority-deferred`, `priority-lift`, `priority-applied`, `priority-failed` and `priority-dropped` |
| `old_groups`, `new_groups` | Every `dev` queue entry, all pages, in queue order, read just before and just after the mutation; `null` when not read or unreadable |
| `observed_between` | The times of the first and last queue reads |
| `rebuilt` | Each pull request in both reads whose group head changed, with `old_head`, `new_head`, `old_base` and `new_base` |
| `removed`, `added` | Pull requests only in the first read, or only in the second |
| `gaps` | Why a queue read failed, as `queue unreadable: <error>` |
| `errors` | The GitHub errors the mutation returned; the after read still runs when the mutation fails |
| `steps` | `priority` only: each `["dequeue", url]`, `["queue", url]` and `["restore", url]` in the order sent |

Each queue group lists `pull`, `pull_head`, `position`, `state`, `jump`, `enqueued_at`, `enqueuer`, `head` (the merge group commit) and `base`. An entry still waiting for its group has a `null` head. `rebuilt`, `removed` and `added` appear only when both reads succeeded. Records from the `ready` merge pass also carry `swarm`, `task`, `timer` (the tick process ID) and `by: "merge watcher"`. Deferred priority records carry the request's `repo`, `group_head`, `run_id`, `sender` and `order`; `priority-dropped` and `priority-requeue-skipped` carry `reason`.

`rebuilt` shows only what changed between two reads taken seconds apart. GitHub rebuilds groups after the mutation returns, so most records show no rebuild, and an entry that gained its first group head counts as rebuilt from `null`. To find the mutation behind a rebuild, search by time, as below.

Count records by action:

```bash
jq -r .action ~/.agentihooks/merge-queue-events.jsonl | sort | uniq -c
```

Read the last priority insertion and the order it sent:

```bash
jq -c 'select(.action == "priority") | {at, token_actor, agent, pull, steps, errors}' \
  ~/.agentihooks/merge-queue-events.jsonl | tail -1
```

List every swarm mutation inside a cancelled run's window, taking `started` and `at` from its watcher record:

```bash
jq -c --arg from "<started>" --arg to "<at>" \
  'select(.at[0:19] >= $from[0:19] and .at[0:19] <= $to[0:19]) | {at, action, agent, pull, steps}' \
  ~/.agentihooks/merge-queue-events.jsonl
```

An empty result means no swarm tooling on this machine changed the queue while the run built.

### The watcher record

The `queue-fail-fast` job of every merge group Tests run polls its queue ref. When the ref no longer points at the run's commit, the group was rebuilt and the run can no longer merge. Before cancelling, the job runs `python3 -m scripts.ci_queue_rebuild <observed ref head>`, which reads, on the job's App token:

1. the run's start time and the first parent of the run's commit (`old_parent`),
2. every page of the `dev` merge queue,
3. the last 20 merge queue add and removal events of this pull request, and
4. the pull request whose merge group run built `old_parent`, and its last 20 queue events.

The App token carries `permission-pull-requests: read` for these reads. Each read times out after 20 seconds.

The record is printed on one log line beginning `queue-rebuild ` and appended to the job summary under the heading `Queue rebuild queue-rebuild-<run id>-<attempt>`. Its fields:

| Field | Meaning |
|---|---|
| `id` | `queue-rebuild-<run id>-<run attempt>` |
| `at`, `started` | The record time and the run's start time, which bound the event window |
| `ref`, `pull` | The queue ref and the pull request it names |
| `old_head`, `old_parent` | The cancelled run's commit and its first parent: the previous group head in a chain, not necessarily `dev` |
| `observed_ref_head` | The ref head the watcher saw |
| `new_head`, `new_base`, `expected_head` | This pull request's current group head and base, and its pull request head; `null` when it left the queue |
| `cause` | The linked cause below |
| `groups` | The whole queue, in the journal's group format |

Only queue events inside the run window, deduplicated by event ID, name a cause. The first row that matches decides:

| `cause.action` | When | Also carries |
|---|---|---|
| `dequeue` | The pull request left the queue and was removed during the run | `actor`, `reason`, `at`, `events` |
| `unknown` | The pull request left the queue with no removal event | `gap` |
| `requeue` | It was removed and added again during the run, the add last | `actor` of the add, `removed_reason`, `at`, `events` |
| `unknown` | The pull request ahead could not be read | `gap` |
| `removed_ahead` | The pull request whose group built `old_parent` was removed during the run | `pull`, `actor`, `reason`, `at`, `events` |
| `unknown` | Entries ahead were enqueued during the run | `gap`, and `correlated`: each entry's `pull`, `enqueuer`, `enqueued_at` and `jump` |
| `unknown` | None of the above | `gap` naming what queue entries cannot show: a removal ahead with no queue run at the old parent, a base push or a ruleset change |

A correlated entry is evidence, not a cause: GitHub records no event linking an enqueue ahead to the rebuild it caused. Any failed read makes the cause `unknown` with the gap `queue unreadable: <error>`.

The cancellation names the record. From run [39708426310](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39708426310):

```text
FAILED: queue group is no longer current (record queue-rebuild-39708426310-1: unknown cause: no entry ahead was enqueued and this pull request was not removed while the group ran; ...)
```

The other causes read `dequeue by <actor> (<reason>)`, `requeue by <actor>` and `removal of <url> ahead by <actor> (<reason>)`; a correlated unknown appends `; jump of <url> by <login> at <time>` or `; enqueue of <url> by <login> at <time>` for each entry. When the record cannot be produced at all the cancellation reads `queue group is no longer current (record unavailable)`, and the run is still cancelled.

Read a record from its job log:

```bash
gh api repos/<owner>/<repo>/actions/jobs/<queue-fail-fast job id>/logs \
  | grep -m1 'queue-rebuild {' | sed 's/.*queue-rebuild //' | jq '{id, started, at, old_head, cause}'
```

### Limits

- A native enqueue, jump or dequeue made outside the swarm tooling, from the GitHub interface or a plain `gh` call, is journaled by nothing; the watcher sees it only through queue events and entries when a rebuild cancels a run.
- A removal ahead whose group never ran at `old_parent`, a base push and a ruleset change stay `unknown` with that gap.
- The journal lives only on the machine that ran the tooling. The watcher record lives only in the run log and job summary.
- Journal records are not deduplicated against GitHub events: each mutation call writes one record.
- `merge priority` reorders with dequeue and requeue, never GitHub's native `jump`, so its insertions appear as `steps`, not as `jump: true` entries.

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
