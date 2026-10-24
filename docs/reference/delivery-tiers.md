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

## Hierarchy CPU proof at promotion

The Tests workflow calls `Hierarchy CPU proof` as `hierarchy-cpu` on a push to
`dev`. It compares hierarchy request CPU and response identity and runs paired
ledger load measurements. A hierarchy cost regression therefore receives a
grade before that head can reach `main`. The
[hierarchy grading reference](ledger-load-gate.md#hierarchy-cpu-grading) explains
the CPU ratio, same code margin, response checks and retained pair artifacts;
the [paired load grade](ledger-load-gate.md#grading-after-the-fix) explains the
separate load comparison.

| Event | Hierarchy proof | Effect |
|---|---|---|
| Push to `dev` admitted by promotion concurrency | Called by Tests | `promote` requires `hierarchy-cpu` to succeed |
| Pull request or merge group | Skipped | Never holds entry to the queue or merging into `dev` |
| Manual dispatch of Tests | Skipped | Dispatch `Hierarchy CPU proof` itself for an investigation |
| Manual dispatch of `Hierarchy CPU proof` | Runs on the selected ref | Evidence only; this workflow does not promote |

`hierarchy-cpu` is a direct need of `promote`, with an explicit
`needs.hierarchy-cpu.result == 'success'` check. It is outside the
`Gate — Required` dependency list. A failed, cancelled or skipped hierarchy proof
cannot authorize promotion even if `Gate — Required` is green. `main` retains
its last promoted tree while ordinary pull requests keep merging into `dev`.

### Scheduling and runner isolation

Tests uses the `promotion-dev` concurrency group for dev pushes, `queue: single`
and `cancel-in-progress: false` for that event. A running run finishes; newer
pushes replace only the pending run. Thus a burst grades the running head and
the newest pending head, rather than every intermediate revision. A running
proof is not cancelled merely because another pull request merges.

When called by Tests, the proof uses `hierarchy-cpu-<caller run id>` as its own
concurrency group, avoiding the caller's lock. A standalone dispatch uses one
of the three `hosted-lane-*` groups with `queue: max`, as described in
[CI timing](../ci-timing.md).

For trusted executions, the four current hierarchy pairs and five load pairs
use `anton-oss-batch`; `grade` uses `anton-oss-light`. Other executions fall back
to `ubuntu-latest` under the workflow's actor and repository trust checks.
These jobs never select the pull request `anton-oss` or queue
`anton-oss-queue` runner pools. Redis starts inside each pair job, using the
runner's installed binary when available; see
[Redis acquisition](ledger-load-gate.md#hierarchy-cpu-redis-acquisition).
Runner pool isolation does not imply zero waits or independent physical hosts.

### Configuration and investigation

The promotion call has no operator setting to enable. Its workflow wiring is:

{% raw %}
```yaml
hierarchy-cpu:
  if: ${{ !cancelled() && github.event_name == 'push' && github.ref == 'refs/heads/dev' }}
  uses: ./.github/workflows/hierarchy-cpu.yml
```
{% endraw %}

The reusable proof accepts one optional string input, `base`, for both
`workflow_call` and `workflow_dispatch`:

| Input | Hierarchy comparison base | Paired load comparison base |
|---|---|---|
| Explicit `base` revision | That revision | That revision |
| Empty on a dev push | `github.event.before`, the previous dev head | Pinned revision `726353637ab44da81bdbd374ec393db275856e22` |
| Empty on a standalone dispatch | Merge base of HEAD and `origin/dev` | The same pinned revision |

The hierarchy base on a push is the previous pushed head, which can differ
from the last head promotion actually graded when pending runs were replaced.
To compare a branch normally, or to measure identical code on both sides:

```bash
R=the-cloud-clockwork/agentihooks
BRANCH=your-branch
gh workflow run hierarchy-cpu.yml -R "$R" --ref "$BRANCH"
HEAD_SHA=$(gh api "repos/$R/commits/$BRANCH" --jq '.sha')
gh workflow run hierarchy-cpu.yml -R "$R" --ref "$BRANCH" -f base="$HEAD_SHA"
```

Run the workload in CI. A standalone green result supplies investigation
evidence; the dev push's own successful proof remains the promotion requirement.
Changing runner labels, the promotion condition or grading margins requires a
reviewed workflow change. Current margins and same code records are documented
under [hierarchy grading](ledger-load-gate.md#current-behavior).

Inspect the latest completed dev push, including jobs beyond the first page:

```bash
R=repos/the-cloud-clockwork/agentihooks
RUN=$(gh api "$R/actions/workflows/test.yml/runs?event=push&branch=dev&status=completed&per_page=1" --jq '.workflow_runs[0].id')
gh api --paginate "$R/actions/runs/$RUN/jobs?per_page=100" \
  --jq '.jobs[] | select(.name | startswith("hierarchy-cpu /") or startswith("promote /")) | {id,name,conclusion,labels,runner_name,created_at,started_at,completed_at}'
```

Use a returned job id to read its logs with
`gh api "$R/actions/jobs/$JOB/logs"`, after setting `JOB` to that id.
For measurements, retain the run ids, job timestamps and runner labels for
each named before and after cohort. Separate whole Tests wall time from proof
duration and from runner wait (`started_at` minus `created_at`). Pool waits
alone do not attribute a slowdown to the hierarchy proof.

### Delivery evidence and limits

[Pull request 5223](https://github.com/the-cloud-clockwork/agentihooks/pull/5223)
delivered the promotion dependency, callable proof and batch runner selection.
Its tests only [red run 39408550884](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39408550884)
failed seven new workflow contract cases. The
[green Tests run 39427913795](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39427913795)
and [standalone proof 39427973115](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39427973115)
passed. The merged head's first
[dev push run 39453177041](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39453177041)
passed its hierarchy, five load and grade jobs; the pair jobs reported batch
runner labels and the grader reported the light pool. That delivery used one
hierarchy job; the current four pair matrix came later.

The delivered pull request records an eight run before cohort with median
Tests wall time about 1130 seconds and median maximum pull request and queue
pool wait about 45 seconds. It leaves the five dev push after cohort pending.
These records establish automatic execution and runner selection, not a
completed before and after timing comparison or a ten minute runtime guarantee.

### Troubleshooting

| Symptom | Check and action |
|---|---|
| `Gate — Required` is green but `promote` is skipped | Read `hierarchy-cpu / grade` and all other promotion dependencies. The hierarchy proof must succeed independently of the gate |
| A hierarchy pair or load pair fails before grading | Read the failing job's Redis readiness, dependency installation and probe logs; setup failure is not a measured CPU regression |
| `grade` reports a CPU or response regression | Follow [hierarchy troubleshooting](ledger-load-gate.md#troubleshooting-a-hierarchy-red), retain pair artifacts and fix through a pull request into `dev` |
| A dev push has no proof jobs | Check whether it was cancelled while pending in `promotion-dev`; inspect the running or newest admitted run. A pull request, merge group or Tests dispatch skips the proof by design |
| Proof jobs queue while merge queue jobs run | Compare the returned labels and runner times. Batch jobs do not request queue runners; investigate the named pool's capacity before attributing the wait |

A red promotion receives a fixer task under the [delivery tiers rule](#rules).
It holds release promotion only; repeatedly rerunning a measured regression
until it passes does not repair it.

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
| `full` | The change touches a harness file or a `conftest.py`, the selected tests would need more than twelve legs, or no test was selected and the change is not documentation only | `split`, `unit` and `shard-check`, the whole suite |
| `selected` | One or more tests were selected and fit in twelve legs | `unit-selected`, one job per leg of about 180 stored seconds, on the `files_<leg>` line of the `selection` artifact the `select` job uploads |
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
- **A red promotion must produce one fixer task per failing check.** Each must be a `ci` task naming the job, the run and the commit, raised with a ledger alert and closed by the next green run. The fix is an ordinary pull request. Failed jobs that lost their runner have no code cause and share one [runner outage task](../pillars/swarm.md#runner-outage-runs) instead. A green run also releases the failing check a fixer nobody is working still holds, so the next red run of that check files a new fixer; see [Held causes and the green run](../pillars/swarm.md#held-causes-and-the-green-run).
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

## Queue shard sizing

A merge group runs its unit suite as one wave of shards on the `anton-oss-queue` runner pool. The queue shard planner, `tests.queue_shards`, never plans more shards than that pool holds, so no shard waits for another shard's runner to free up.

### How the plan is built

The `split` job's `Size queue shards from median durations` step runs only on `merge_group` events, once per Python version in the matrix (3.12 today):

```bash
python -m tests.queue_shards --root . --collection collected.json --version "$PYTHON_VERSION" --reused reused.json --output queue-plan.txt
python -m tests.queue_shards combine --from-output queue-plan.txt --root . --inputs queue-input
```

1. The first command prices every collected test from the median of the recent dev durations, drops tests the queue reuses, and starts at 12 shards. It adds one shard at a time until the predicted slowest shard is under half the step timeout, or until it reaches `MAX_SHARDS`. It writes `queue_312=<legs>` to `queue-plan.txt` and prints `Python 3.12: <n> queue shards, predicted slowest <s>s of <half>s`.
2. `combine` reads the plan back and grades it against the pool. The `unit` matrix takes its `include` list from that output.

| Setting | Value | Where it comes from |
|---|---|---|
| `MAX_SHARDS` | 30 | The `maxRunners` of the antoncore `anton-oss-queue` scale set, less the queue tier jobs that run during the unit stage (none today) |
| `STEP_SECONDS` | 720 | The `timeout-minutes` of the `unit` job's `Run tests` step, 12 minutes, read from `.github/workflows/test.yml` at import; see [Step timeout limits](#step-timeout-limits) |
| Half step timeout | 360 s | The target a plan must predict under |
| `SLOWDOWN` | 1.85 | The one margin: factor applied to the predicted slowest shard from median durations, calibrated so the prediction lands within 15 % of the slowest `Run tests` step of ten recorded merge groups |
| `STARTUP_SECONDS` | 30 | Added to every shard's prediction |

### What combine decides

Every plan that is not refused ends with the line `<n> queue jobs fit one wave of 30 runners; predicted slowest test step <s>s`; a capped plan prints its warnings before it.

| Plan | Outcome | Log line |
|---|---|---|
| At most 30 jobs, slowest under 360 s | Runs as one wave | `<n> queue jobs fit one wave of 30 runners; predicted slowest test step <s>s` |
| At most 30 jobs, slowest at or over 360 s | The step fails and the merge group goes red | `Queue plan exceeds 360s of the step timeout: <s>s` |
| More than 30 jobs, from `--from-output` with `--root` and `--inputs` | Replanned at 30 shards for every version and written back to `queue-plan.txt` | `::warning::<n> queue jobs exceed the pool of 30; capped at 30 shards` |
| Capped plan whose slowest shard reaches 720 s | Still runs at 30 shards, with a warning | `::warning::The capped queue plan predicts <s>s, over 720s of the step timeout` |
| More than 30 jobs from `--plans` | Refused | `<n> queue jobs exceed the pool of 30` |

The capped path caps the total across Python versions, not each version, so two versions share the 30 runners. With 3.12 alone the first command already stops at 30, so a plan that cannot reach 360 s at 30 shards is refused rather than capped. A single test too slow to fit any shard produces that refusal.

### Changing the pool

`MAX_SHARDS` must equal the queue pool `maxRunners` less any queue pool job that runs at the same time as the unit stage. When the antoncore scale set changes, change `MAX_SHARDS` in `tests/queue_shards.py` by pull request in the same window. Raising the pool is an antoncore change and stays with the operator. `tests/test_queue_shards.py` pins the cap: `test_a_forty_job_queue_plan_is_capped_to_the_thirty_runner_queue_pool` plants a 40 job plan and expects `40 queue jobs exceed the pool of 30; capped at 30 shards`.

### Checking a queue shard run

Read the plan from the `split` job log, then list the unit shards and their runner waits:

```bash
run="<merge group run id>"
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$run/attempts/1/jobs" --paginate \
  --jq '.jobs[] | select(.name | startswith("unit (")) | [.name, .created_at, .started_at, .completed_at] | @tsv'
```

A shard's runner wait is `started_at` minus `created_at`. Count the shards: more than 30, or start times that fall into two groups minutes apart, means a second wave. The Doctor measure prints the run attempt's queue tier wall time in seconds:

```bash
agentihooks doctor rig-grade-swarm measure "ci-tier-wall-time/<run id>-<attempt>"
```

It prints `<finding> <seconds>`, for example `ci-tier-wall-time/39269601765-1 573`. A finding fires when the tier wall time passes 15 minutes, 900 s. A run with no recorded tier wall time is refused with `has no tier wall time`.

### Concurrent merge groups on one pool

The shard cap keeps one merge group inside one wave, but the queue then built up to three entries at a time (`max_entries_to_build` 3; `.github/rulesets/dev-no-delete.json` now sets 1) and every group sent its shards to the same `anton-oss-queue` pool. Two groups that reach their unit stage together ask for up to 60 runners, so the later group's shards wait for the earlier group's shards to finish. That wait, not the shard plan, is what pushed merge group tiers over 15 minutes on the night of 19 to 20 October.

Doctor finding `ci-tier-wall-time/39346819295-1` fired on merge group run [39346819295](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39346819295), which took 911 s: 201 s before the unit stage, 599 s in it and 111 s after it. Its 30 shards were created while 27 shards of merge group run 39346300411 were still waiting or running, a demand of 57 on the 30 runners the pool held then. Its shards waited a median 128 s and up to 293 s for a runner.

The cause task read every merge group Tests run from 18:00 to 01:30 UTC through the jobs API: 130 runs, 88 run attempts with unit shards. Each attempt ran 30 unit shards in 80 of 88 cases, plus three other queue jobs (the two `test-count` legs and `split`) that finished before the first shard was created, so no other queue job competed with the unit stage. Attempts are grouped by how many shards of other groups were waiting or running when their own unit stage started:

| Other groups' shards in flight | Attempts | Median shard wait | Worst shard wait, median and max | Unit stage median | Green first attempts over 15 minutes |
|---|---|---|---|---|---|
| Under 10, alone | 24 | 18 s | 119 s, 224 s | 373 s | 2 of 17 |
| 10 to 40, one other group | 42 | 124 s | 266 s, 471 s | 480 s | 9 of 31 |
| Over 40, two or more groups | 22 | 331 s | 518 s, 713 s | 702 s | 10 of 15 |

Two other ways of counting a group as concurrent, the peak of other shards during its own wait or any other queue job overlapping that wait, move attempts between rows but keep the same rise: the alone median shard wait is 18 s under all three, and two or more groups put 11 of 18 and 14 of 25 green first attempts over 15 minutes.

Across the night 27 to 30 queue runners were busy 57 percent of the time, and 67 percent of all queue job wait minutes (4181 of 6227) passed with 28 or more busy. Demand, waiting plus running, was over 30 for 63 percent of the time, over 60 for 14 percent and peaked at 90. On an idle pool a shard was picked up in a median 19 s. The queue built 17.4 groups an hour, of which 8.4 went green; 64 of the 130 runs were cancelled and used 2333 of the 10058 queue runner minutes. With a median green tier of 788 s for a group alone, one group at a time would serve about 4.6 groups an hour.

The cause is recorded in issue [5037](https://github.com/the-cloud-clockwork/agentihooks/issues/5037). The task was read only: it changed neither the pool nor `MAX_SHARDS`, and wrote the numbers above onto the operator's ledger follow up. The remedy among a bigger pool, one group building at a time, or a shard plan sized to the runners actually free stays with the operator.

To measure overlap for a window, list the merge group runs and then each run's unit shards with their creation and start times:

```bash
since="2026-10-19T18:00:00Z"
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs?event=merge_group&created=>=$since&per_page=100" --paginate \
  --jq '.workflow_runs[] | select(.name == "Tests") | [.id, .run_attempt, .created_at, .conclusion] | @tsv'
run="<merge group run id>"
attempt="<attempt>"
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$run/attempts/$attempt/jobs?per_page=100" --paginate \
  --jq '.jobs[] | select(.name | startswith("unit (")) | [.name, .created_at, .started_at, .completed_at] | @tsv'
```

For the finding run, the runs listing with `since` set to `2026-10-20T00:10:00Z` prints, among others:

```text
39346819295	1	2026-10-20T00:18:07Z	success
39346300411	1	2026-10-20T00:12:53Z	success
```

and the jobs listing for run `39346819295`, attempt `1`, prints 30 shard rows such as:

```text
unit (3.12, 2)	2026-10-20T00:21:28Z	2026-10-20T00:23:23Z	2026-10-20T00:28:45Z
unit (3.11, 5)	2026-10-20T00:21:28Z	2026-10-20T00:21:44Z	2026-10-20T00:25:36Z
```

That night's groups still ran Python 3.11 and 3.12 legs inside the same 30 shards; 3.11 left CI later.

A group's other shards in flight at its unit start is the number of other groups' unit shards whose `created_at` is before that group's first shard `created_at` and whose `completed_at` is after it. A group whose shards wait over a minute with that number over 10 is sharing the pool with another group; a wait with that number under 10 does not come from another merge group.

The queue pool changed after the finding: antoncore pull request [1036](https://github.com/the-cloud-clockwork/antoncore/pull/1036), merged 21 October, sets `anton-oss-queue` to `minRunners` 12 and `maxRunners` 18 in `values-oss-queue.yaml`, while `MAX_SHARDS` in `tests/queue_shards.py` is still 30. Compare the two before reading a shard wait against the numbers above.

### Troubleshooting queue shard sizing

| Symptom | Cause | Fix |
|---|---|---|
| `Queue plan exceeds 360s of the step timeout` | 30 shards cannot bring the slowest shard under half the step timeout, often one slow test or a slow serial group | Split or speed up the slow test file; the planner log names the predicted seconds |
| Shards wait minutes for a runner while each group plans 30 or fewer | Several merge groups build at once and share the 30 runners; the queue builds up to three entries at a time | Read the runner waits of every group running in the same minutes before changing the plan; see [Concurrent merge groups on one pool](#concurrent-merge-groups-on-one-pool) |
| More than 30 `unit` jobs in one run | `MAX_SHARDS` no longer matches the pool, or a second Python version was added without the capped path | Compare `MAX_SHARDS` with the antoncore scale set `maxRunners` |

### Delivery evidence

Delivered by pull request [4803](https://github.com/the-cloud-clockwork/agentihooks/pull/4803) for issue [4801](https://github.com/the-cloud-clockwork/agentihooks/issues/4801), which lowered `MAX_SHARDS` from 40 to 30. Before: merge group run [39249741419](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39249741419) planned 40 unit shards on the 30 runner pool, 10 waited for a second wave with runner waits up to 346 s, and the queue tier took 948 s. After: the first successful merge group run started after the merge, [39269601765](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39269601765), ran 30 unit shards in one wave, all started within 63 s, with a 308 s unit stage and a 574 s tier wall. Runner wait was median 29 s and worst 82 s; 8 of 30 shards still waited over 30 s, which overlapping merge groups explain. Later changes on the same planner balanced capped shards by predicted slowest shard ([4884](https://github.com/the-cloud-clockwork/agentihooks/pull/4884)), read the step timeout from the workflow ([5043](https://github.com/the-cloud-clockwork/agentihooks/pull/5043)), dropped Python 3.11 from CI ([5327](https://github.com/the-cloud-clockwork/agentihooks/pull/5327)) and calibrated `SLOWDOWN` ([5659](https://github.com/the-cloud-clockwork/agentihooks/pull/5659)).

## Step timeout limits

The queue shard planner and the shard budget grade shards against the time their test step may run. Both read that time from one source, the `timeout-minutes` of the step in `.github/workflows/test.yml`, so a timeout change in the workflow moves the planner target and the budget in the same commit.

| Reader | Job | Step | Timeout today | Limit it sets |
|---|---|---|---|---|
| `tests.queue_shards` (`STEP_SECONDS`) | `unit` | `Run tests` | 12 minutes | 720 s; a queue plan must predict its slowest shard under half of it, 360 s |
| `tests.shard_budget`, a unit shard | `unit` | `Run tests` | 12 minutes | 720 s of elapsed test step time |
| `tests.shard_budget`, a selected leg | `unit-selected` | `Run the selected tests` | 7 minutes | 420 s of elapsed test step time |

### How a limit is read

`step_seconds(job)` in `tests/shard_budget.py` maps the job to its step name (`STEPS`: `unit` to `Run tests`, `unit-selected` to `Run the selected tests`), takes the workflow text from that job's header on, finds the first step of that name after it, keeps that step's text up to the next step, and returns the step's `timeout-minutes` times 60. A `timeout-minutes` line of a later step is never read. `tests.queue_shards` imports it and sets `STEP_SECONDS = step_seconds("unit")` when the module loads, so the planner needs no YAML library.

The reader expects the plain form the workflow uses today:

```yaml
  unit:
    steps:
      - name: Run tests
        timeout-minutes: 12
```

A renamed job or step, a step without `timeout-minutes`, or a timeout written as an expression stops the read with a Python error: `IndexError: list index out of range` when the job or step name is not found, `TypeError: 'NoneType' object is not subscriptable` when the step holds no integer `timeout-minutes` line. For the planner that error happens at import, so the merge group `split` job fails before it plans. Rename the step and its entry in `STEPS` in the same pull request.

### Elapsed time records

Both test steps time pytest with the shell's `SECONDS` and write `elapsed.json` next to `durations.json`, keeping pytest's exit status, so a failed shard still records its time:

| Job | `elapsed.json` | Uploaded in |
|---|---|---|
| `unit` | `{"seconds": <n>}` | `durations-<python>-<shard>`, with `durations.json` and `durations.sha256` |
| `unit-selected` | `{"seconds": <n>, "job": "unit-selected"}` | `durations-<python>-<leg>`, with `durations.json` |

The budget grades each shard on its `elapsed.json` seconds and picks the step from its `job` field, `unit` when the field is absent. A folder with `durations.json` and no `elapsed.json` is graded on the sum of its test durations against the `Run tests` limit.

### Running the shard budget

`shard-check` runs it as its last step, `Check every shard's test time fits the Run tests deadline`, on every event except `merge_group`, over every downloaded `durations-3.12-*` artifact:

```bash
python -m tests.shard_budget $REPORT shard-durations/*/durations.json
```

| Event | `REPORT` | A shard over its limit |
|---|---|---|
| `push` to `dev` | `--report` | `::warning::`, the step stays green |
| Full scope pull request, dispatch | empty | `::error::`, `shard-check` fails |
| `merge_group` | step skipped | Not graded; shard budgets never hold the queue |

A shard folder that holds no `durations.json` fails the step in both modes: `::error::No shard durations at <paths>, so those shards cannot be graded.`

`unit-selected` runs only on pull requests of `selected` scope, and `shard-check` runs on pull requests only at `full` scope, so the workflow never grades a selected leg today. The selected limit applies when a selected leg's artifact is graded by hand. Download it and run the command from a checkout of the same workflow:

```bash
run="<pull request Tests run id>"
gh run download "$run" --pattern 'durations-3.12-*' --dir shard-durations
python -m tests.shard_budget shard-durations/*/durations.json
```

The first line names the slowest shard and the limit of its own step; one line per shard follows, slowest first:

```text
2 shards, slowest 431.0 s of test time against the 7 minute Run the selected tests deadline of 420 s
durations-3.12-1 431.0 s over budget
durations-3.12-2 120.0 s
::error::1 of 2 shards passed the 420 s budget.
```

When shards over budget have different limits, the last line joins them, for example `passed the 420 or 720 s budget`. `--budget <seconds>` replaces every step limit with one number, and the first line then reads `against a budget of <seconds> s`.

### Tests that pin the limits

| Test | Pins |
|---|---|
| `test_the_planner_step_timeout_is_the_unit_run_tests_step_timeout` in `tests/test_queue_shards.py` | `STEP_SECONDS` equals the `Run tests` timeout |
| `test_the_default_budget_is_the_run_tests_step_deadline` in `tests/test_shard_budget.py` | A unit shard is graded against the `Run tests` timeout |
| `test_a_selected_leg_is_graded_against_the_selected_tests_step_deadline` in `tests/test_shard_budget.py` | A leg whose `elapsed.json` names `unit-selected` is graded against the `Run the selected tests` timeout |
| `test_selected_legs_record_elapsed_time_and_their_job_and_preserve_pytest_status` in `tests/test_ci_workflow.py` | The selected step writes `elapsed.json` with its job and keeps pytest's exit status |
| `test_unit_step_records_elapsed_time_and_preserves_pytest_status` in `tests/test_ci_workflow.py` | The unit step writes `elapsed.json` and keeps pytest's exit status |

### Troubleshooting step timeout limits

| Symptom | Cause | Fix |
|---|---|---|
| `split` fails with `IndexError: list index out of range` from `step_seconds` | The `unit` job or its `Run tests` step was renamed | Update `STEPS` in `tests/shard_budget.py` with the new name |
| `TypeError: 'NoneType' object is not subscriptable` from `step_seconds` | The step's `timeout-minutes` was removed or written as an expression | Keep a plain integer `timeout-minutes` on the step |
| The queue plans more shards than the step timeout needs | An older planner with a fixed timeout; before pull request 5043 it assumed 420 s while the step allowed 720 s | Merge `origin/dev`; the `split` log prints `of 360s` while the timeout is 12 minutes |
| A selected leg passes 420 s and nothing turns red | `shard-check` never runs beside `unit-selected` | Grade the leg's artifact by hand as above |
| `KeyError` from `tests.shard_budget` | An `elapsed.json` names a job other than `unit` or `unit-selected` | Add the job and its step to `STEPS` |

### Delivery evidence

Delivered by pull request [5043](https://github.com/the-cloud-clockwork/agentihooks/pull/5043) for issue [5041](https://github.com/the-cloud-clockwork/agentihooks/issues/5041). Before it, the planner held a fixed 420 s timeout while `Run tests` stopped at 720 s, and the budget graded every shard, selected legs included, against a fixed twelve minutes of summed test time, though `Run the selected tests` stops at seven minutes and recorded no elapsed time. Red: Tests run [39357706899](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39357706899), on the test only commit, failed exactly the six new or changed tests. Green: Tests run [39358460209](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39358460209).

Measured over completed merge group Tests runs, counting Python 3.12 unit shards per group and the slowest `Run tests` step: the last ten groups before the merge ran 20 shards each, slowest 388 s; the ten groups after 06:01 UTC on 20 October ran a mean of 13.3 shards (13 or 14), slowest 305 s, and no shard reached 720 s. Calibrating `SLOWDOWN` from 1.5 to 1.15 in pull request [5659](https://github.com/the-cloud-clockwork/agentihooks/pull/5659) later changed the plan again. Pull request [6707](https://github.com/the-cloud-clockwork/agentihooks/pull/6707) for issue [6703](https://github.com/the-cloud-clockwork/agentihooks/issues/6703) dropped the slowest of five durations and set `SLOWDOWN` to 1.85 on median durations, calibrated on merge group runs [40084693315](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40084693315), [40085105981](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40085105981), [40085828452](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40085828452), [40086486432](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40086486432), [40087075078](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40087075078), [40087870160](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40087870160), [40088540093](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40088540093), [40089176680](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40089176680), [40089783935](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40089783935), [40090459054](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/40090459054): at 17 shards they predicted 346 to 353 s against a measured slowest `Run tests` step of 299 to 378 s, 8 of 10 within 15 %; on median durations the same groups predict 331 to 338 s, all 10 within 15 %.

## Grouped test files in the unit shard planner

The unit shard planner, `assign_nodes` in `tests/shards.py`, places the measured tests of one test file that carries an `xdist_group` mark on one shard, as one unit, and charges their summed time to that shard. Inside the shard, the group's tests run on one worker in collection order. Every other test is still placed on its own by its stored duration.

A group's later tests reuse what its first test built: a module scoped fixture, a cached build or a warmed import. Split across shards, each shard builds it again; reordered by duration, a later test can run before the test that builds what it needs. The remote control mutation baseline file is the case that needed it: it runs as two tests in the `remote_control_mutation` group, one that collects the statistics baseline and one that feeds it to mutant grading, so the costly baseline is built once and stays under the one minute test watchdog.

The same planner serves every sharded unit run:

| Caller | Runs on | What it plans |
|---|---|---|
| `--shard N/M` collection, `_shard_files` and `pytest_collection_modifyitems` in `tests/conftest.py` | Each `unit (<python>, <shard>)` job, with `--dist loadgroup` and one worker per usable CPU (`scripts.ci_cpu_quota`); the plan itself assumes four | Which test files the shard collects and which collected tests it keeps |
| `tests.queue_shards` | The merge group `split` job | The predicted slowest shard used for [queue shard sizing](#queue-shard-sizing) |

### How a grouped file is placed

1. `grouped_files` parses every test file and records the files that call `xdist_group` anywhere, as a module `pytestmark` or on a single test. The value is the group name, or the sorted names joined by `_` when a file names several. The planner treats every test in such a file as part of its group, marked or not.
2. `assign_nodes` computes an even shard share: the total stored duration of every test it plans, divided by the shard count.
3. A grouped file whose summed duration is at or under that share becomes one unit holding all its measured tests in stored order. A grouped file over the share is split test by test, since no shard can hold it; each of its tests still counts against its shard's group load.
4. Units are placed slowest first on the shard whose predicted time grows least. A shard's predicted time is the largest of its summed time over the workers, its heaviest group, and the receiving group's load plus the new unit, plus the source size cost of each file it collects for the first time. A grouped unit therefore adds its whole summed time to the group load of one shard, which models the one worker that `--dist loadgroup` gives the group.
5. Files in `FIRST_SHARD_FILES` (today `tests/routing/test_codex_api.py`) are left out of the plan and always run on shard 1.

Inside each worker, `slowest_first` orders the collected tests slowest first, with tests under 0.1 s after them in collection order. Every test of a group takes the group's slowest duration, and the sort is stable, so the group keeps collection order at the place of its slowest test. xdist appends `@<group>` to a grouped test id; the planner strips it before reading stored durations, including ids whose parameters hold an `@`.

Tests with no stored duration follow their file. A test id the plan does not name goes to a shard that already holds measured tests of its file, chosen by a CRC32 of the test id; a grouped file kept whole has exactly one such shard, so its new tests join it. A test file with no measured test at all is collected by every shard and each of its tests is hashed across all shards, so a new grouped file can split until the next dev push records its durations. Since the split job prices every collected test before planning (see [stored duration coverage](#stored-duration-coverage)), this fallback only catches a test id missing from the split job's `.test_durations`.

### Grouped file example

The planner contract test `test_one_files_xdist_group_stays_on_one_shard_with_its_summed_time` in `tests/test_shards.py` pins the behaviour with two shards and four workers:

```python
durations = {
    "tests/test_g.py::test_builds": 5.0,
    "tests/test_g.py::test_reuses": 30.0,
    "tests/test_a.py::test_x": 20.0,
    "tests/test_b.py::test_y": 20.0,
}
assign_nodes(durations, 2, {"tests/test_g.py": "g"}, 4)
# [["tests/test_g.py::test_builds", "tests/test_g.py::test_reuses"],
#  ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]]
```

The share is 37.5 s, so the 35 s group stays whole on one shard in collection order and the two 20 s tests share the other. Before the change the planner placed each test alone by duration and put the 30 s `test_reuses` on one shard and the 5 s `test_builds` on the other. `test_a_grouped_file_longer_than_an_even_shard_share_still_splits` pins the cap: four 30 s tests of one group on two shards have a 60 s share against a 120 s file, so they split two and two.

### Grouped file configuration

The planner has no setting of its own. Its inputs are:

| Input | Where it comes from |
|---|---|
| Group membership | Any `pytest.mark.xdist_group(...)` call in a test file, read from source with `ast` |
| Stored durations | `.test_durations` at the repository root, which each unit job rewrites from the latest dev durations of its Python version (`tests.dev_durations.adopt`: the shared file overlaid with `.test_durations-<version>`) when a dev durations cache exists |
| Shard count | `--shard N/M`: 20 for the 3.12 unit job, or the queue plan's `total` on merge groups |
| Workers | 4 in the planner call in `tests/conftest.py` and `WORKERS` in `tests/queue_shards.py` |
| Source size cost | `SECONDS_PER_SOURCE_BYTE` in `tests/shards.py` |

To keep a set of tests together, put them in one file with one group:

```python
import pytest

pytestmark = pytest.mark.xdist_group("remote_control_mutation")
```

Keep the mark off fakeredis test modules; the [suite speed checks](#suite-speed-checks-on-pull-requests) refuse it there. Files in different modules that share a group name are separate units: the planner may place them on different shards, and on one shard their times add to the same group load.

### Checking a grouped file placement

The planner is plain Python and runs without pytest. From a checkout, this prints the even share and the shard of every test in one grouped file, the same way the 3.12 unit jobs plan it:

```bash
python - tests/test_remote_control_mutation_baseline.py <<'EOF'
import json, sys
from pathlib import Path
from tests.shards import FIRST_SHARD_FILES, assign_nodes, discover_test_files, grouped_files, source_sizes

root, target, shards = Path("."), sys.argv[1], 20
durations = {**json.loads((root / ".test_durations").read_text()), **json.loads((root / ".test_durations-3.12").read_text())}
files = [p for p in discover_test_files(root) if p not in FIRST_SHARD_FILES]
measured = {n: s for n, s in durations.items() if n.split("::", 1)[0] in files}
parts = assign_nodes(measured, shards, grouped_files(root, files), 4, sizes=source_sizes(root, files))
print(f"share {sum(measured.values()) / shards:.1f}s")
for i, part in enumerate(parts, 1):
    for node in part:
        if node.startswith(target + "::"):
            print(i, f"{measured[node]:.1f}s", node)
EOF
```

On `dev` at `eef49b2f30` this reported a 280.4 s share over 5607.4 s of stored 3.12 time, 17 grouped files, 10 of them with stored durations, the largest 41.7 s (`tests/inbox/test_channel_server.py`), and no grouped file on more than one shard. The remote control mutation file sat whole on shard 2 at 40.0 s. Its stored id still named the single test that existed before the split, so its two current tests are unmeasured and follow it there.

To see what a CI shard ran, read the `unit (3.12, <shard>)` job log: the Run tests step prints each test id with `-v`, and every grouped test id ends in `@<group>`.

### Troubleshooting grouped files

| Symptom | Cause | Fix |
|---|---|---|
| Tests of one group appear in two unit shard logs | The file's summed duration is over the even shard share, or none of its tests has a stored duration yet | Compare the file's stored total with the share using the command above; a new file settles after the next dev push stores its durations |
| A group's later test fails because what its first test builds is missing | The tests sit in different files, or the group was split over the share | Move the tests into one file with one group, and keep the file under the share by moving unrelated tests out |
| One shard runs long while carrying a grouped file | The group runs on one worker, so its summed time is that shard's floor | Split the file's work so that it reuses less, or move tests that need no shared state out of the file |
| `shard-check` reports a test run more than once or not at all | A change to the planner's placement or fallback rules broke the exactly once rule | Read `tests/test_shards.py` and the shard check log; the planner returns every planned test exactly once |

### Delivery evidence

Delivered by pull request [5032](https://github.com/the-cloud-clockwork/agentihooks/pull/5032) for issue [5031](https://github.com/the-cloud-clockwork/agentihooks/issues/5031), after pull request [4909](https://github.com/the-cloud-clockwork/agentihooks/pull/4909) split the remote control mutation baseline test into two tests of one group and made `slowest_first` keep a group in collection order. Red: Tests run [39353180256](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39353180256), test only, failed the new contract test on both Python versions. Run [39353833681](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39353833681), on a first fix that kept every grouped file whole, failed the queue sizing tests and the exactly once shard test, which the share cap fixed. Green: Tests run [39355391107](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39355391107), with every unit shard and `shard-check` passing.

Shard balance before the change, read on the first dev push Tests run after 4909 merged, [39338932046](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39338932046), with all unit shards green: on Python 3.11, shard 1 took 184 s against 174 to 294 s for the other eleven, median 187 s; on Python 3.12, 390 s against 358 to 530 s, median 388 s. Shard 1, which held the remote control pair then, was not an outlier. The change targets placement, not shard wall time, and no matching after window on the same twelve shard layout exists: Python 3.11 left CI and the 3.12 unit job moved to 20 shards later.

## Stored duration coverage

Every test the unit shard planner sees has a stored duration. Two writers fill the gaps with the median of the map they write, so a test that no run has timed yet is priced at a typical test's cost. Before this, the planner left such a test out of `assign_nodes` and placed it by the CRC32 fallback without weighing it.

| Writer | Runs in | Fills |
|---|---|---|
| `tests.refresh_durations` | The `Refresh measured durations` step of the dev push refresh job: `python -m tests.refresh_durations --samples ~/durations-samples --ci-run "$GITHUB_RUN_ID" --ci 5` | Each map it writes (`.test_durations` and `.test_durations-<version>`) gets every collected test it lacks, priced at that map's own median. The maps go out as the `durations-merged` artifact and the `durations-merged-<sha>` cache |
| `tests.dev_durations` | The split job's `Adopt latest dev durations` step, and the `Size queue shards from median durations` step on merge groups | After choosing which durations to use, every collected test still missing from `.test_durations` gets the median of that file. The completed file goes out as the `split-<version>` artifact, and the unit shards plan from it |

### How a push picks and completes its durations

1. The split job collects the head's tests (`tests.duration_coverage.collected_tests`), or reads `collected.json` from the selection on pull request and merge group events.
2. `_choose` in `tests.dev_durations` takes the restored dev cache (`.test_durations` overlaid with `.test_durations-<version>`). `validate_coverage` refuses a map that lacks more than one collected test in ten; the step then prints `Refusing the restored dev durations: <n> of <m> tests have no stored duration` and tries the committed `.test_durations-<version>`, then `.test_durations`. When neither file covers nine tests in ten, every collected test without a committed duration is priced at the committed median and the step warns `Splitting tests without a committed duration evenly`.
3. `main` fills each collected test that the chosen map lacks with the median of that map, rewrites `.test_durations`, and writes its sha256 to `durations.sha256`.
4. Each `unit (3.12, <shard>)` job downloads `split-3.12`, and `_shard_files` in `tests/conftest.py` passes every collected test to `assign_nodes` with its stored or median price.

On the dev push refresh the same one in ten check runs first, and the step fails with `::error::refresh durations: <n> of <m> tests have no stored duration` when a written map would lack more than that. When the run's own shard samples are incomplete, the line ends `; run <id> kept no samples from durations-3.12-<shard>`. Within that limit the fill gives a new test the median until a later dev push times it; `ci_medians` then replaces the estimate with a measured value.

The median is small. On the dev push Tests run [39992377126](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39992377126) at `94dcb486bf`, each `durations-merged` map held 43558 tests; the median was 0.0529 s in `.test_durations` and `.test_durations-3.12`.

### Measured effect

The Doctor measured both numbers over ten completed, non cancelled dev push Tests runs on each side of the fix (ledger tasks t553 and t618), from the run artifacts:

- **Unknown duration share**: collected tests in `collected-3.12` that the `.test_durations` in `split-3.12` does not name, over all collected tests.
- **Shard spread**: the slowest Python 3.12 unit shard's `elapsed.json` seconds minus the median shard's, per run, then the median over the ten runs.

| Window | Runs | Shards | Unknown share | Spread median (range) | Slowest shard median | Median shard median |
|---|---|---|---|---|---|---|
| Before: 20 October, 17:12 to 19:40 UTC, ending run [39485406636](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39485406636) | 10 | 12 | 274 of 398884 (0.069%) | 27.25 s (5 to 58) | 325 s | 294.25 s |
| After: 21 October, 00:38 to 03:41 UTC, runs [39522581705](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39522581705) to [39539929506](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39539929506) | 10 | 20 | 0 of 403194 (0%) | 19.0 s (6.5 to 36) | 214.5 s | 195.5 s |

The after window starts with the first dev push Tests run created after the fix was applied, at 00:22 UTC on 21 October. Nine runs before and five after ended in failure; their shards still wrote `elapsed.json`, so they count.

Read the spread with the shard count beside it. Pull request [5342](https://github.com/the-cloud-clockwork/agentihooks/pull/5342) moved the 3.12 unit job from 12 to 20 shards at 21:46 UTC on 20 October, between the two windows, and every shard got shorter. Measured against the median shard, the spread did not change: 9.6% before and 9.9% after (the median of each run's spread over its median shard). The unknown share is the number the fix moved. Each `durations-merged` per version map covered every collected test in both windows; the 274 gaps before the fix were tests that the map the split job adopted did not name yet.

### Measuring a window

This reads the two numbers for any dev push Tests run whose artifacts are still kept: `split-<version>` for 7 days, `collected-<version>` and the shard `durations-*` artifacts for 14. It downloads through `gh api`, so it also works with a `gh` that has no `run download --pattern`:

```bash
cat > coverage_spread.py <<'EOF'
import io, json, re, statistics, subprocess, sys, zipfile

REPO = "repos/the-cloud-clockwork/agentihooks"

def api(path):
    return subprocess.run(["gh", "api", path], check=True, capture_output=True).stdout

def member(archive, suffix):
    return json.loads(archive.read(next(n for n in archive.namelist() if n.endswith(suffix))))

for run in sys.argv[1:]:
    artifacts = json.loads(api(f"{REPO}/actions/runs/{run}/artifacts?per_page=100"))["artifacts"]
    wanted = {a["name"]: a for a in artifacts if re.fullmatch(r"collected-3\.12|split-3\.12|durations-3\.12-\d+", a["name"])}
    files = {name: zipfile.ZipFile(io.BytesIO(api(f"{REPO}/actions/artifacts/{a['id']}/zip"))) for name, a in wanted.items()}
    collected = member(files["collected-3.12"], "collected.json")
    planned = member(files["split-3.12"], ".test_durations")
    elapsed = [member(f, "elapsed.json")["seconds"] for name, f in files.items() if name.startswith("durations-")]
    missing = sum(node not in planned for node in collected)
    median = statistics.median(elapsed)
    print(run, f"missing {missing}/{len(collected)}", f"shards {len(elapsed)}",
          f"slowest {max(elapsed)}", f"median {median}", f"spread {max(elapsed) - median}")
EOF
runs=$(gh api 'repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page=30' \
  --jq '[.workflow_runs[] | select(.conclusion != "cancelled")][:10][].id')
python coverage_spread.py $runs
```

Run on two runs of the after window it printed:

```text
39539929506 missing 0/40380 shards 20 slowest 203 median 191.0 spread 12.0
39522581705 missing 0/40250 shards 20 slowest 251 median 215.0 spread 36.0
```

Take the median of the `spread` column over the ten runs, and divide the total `missing` by the total collected. A run without a `split-3.12` artifact (a run that reused a queue split, or one past its retention) stops the script with `KeyError: 'split-3.12'`; leave that run out of the window and take the next one.

### Troubleshooting stored durations

| Symptom | Cause | Fix |
|---|---|---|
| The refresh job fails with `::error::refresh durations: <n> of <m> tests have no stored duration` | A written map lacks more than one collected test in ten | Read the rest of the line: `kept no samples from` names the shards whose `durations-3.12-<shard>` artifacts the run lost, which leaves their tests unmeasured |
| The split step prints `Refusing the restored dev durations` | The dev cache predates more than a tenth of the suite, for example after a large test move | The committed durations are used instead; the next green dev push refresh restores the cache |
| `Splitting tests without a committed duration evenly` | Neither the cache nor a committed file covers nine tests in ten | Tests without a committed duration are planned at the committed median for this run; the next green dev push refresh restores the cache |
| A new slow test makes one shard run long | It was planned at the median until a dev push timed it | Wait for the next dev push refresh, or check that the test's file appears in that run's `durations-3.12-*` artifacts |
| The measuring script reports missing tests after the fix | The split job did not run `tests.dev_durations` for this run, or read another collection | Read the `Adopt latest dev durations` step log of the split job and compare its `collected.json` with the shards' |

### Delivery evidence

Delivered by pull request [5358](https://github.com/the-cloud-clockwork/agentihooks/pull/5358) for issue [5354](https://github.com/the-cloud-clockwork/agentihooks/issues/5354). Red: refresh proof [39489162638](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39489162638) left out a collected test, and planner proof [39489803250](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39489803250) failed both cache paths. Green: [39490102685](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39490102685) passed all 181 focused checks. `test_refresh_records_every_collected_test_with_a_measured_default` in `tests/test_refresh_durations_errors.py` and `test_planner_prices_every_new_case_from_the_measured_median` in `tests/test_dev_durations.py` pin the two writers.

## Lost unit shard rerun

A merge group unit shard that loses its runner is rerun once inside the same Tests run, so the pull request stays in the queue and merges on the rerun's green.

1. `queue-fail-fast` reads the log of each failed `unit (<python>, <shard>)` job once on the App token. Only an error line `Process completed with exit code <1 to 127>` with no runner loss signature proves a real failure and cancels the run at once; a timed out shard also cancels it. A lost runner can leave no log, or only an annotation the App token cannot read, so any other shard failure waits for the classifier. Every other failed job, the classifier and the rerun included, cancels the run at once. A run whose group was rebuilt is cancelled too, after the watcher writes a [queue rebuild record](#the-watcher-record).
2. `unit-lost` starts when the `unit` matrix ends in failure or cancelled. On the App token it lists this attempt's jobs and runs `python -m scripts.ci_unit_rerun --jobs <listing> --repo <owner>/<repo> --plan <queue plan>`, which judges each failed shard's log by the watcher's rule. A timed out shard, a shard whose log proves a real failure, or a lost leg missing from the queue shard plan makes `unit-lost` fail and the queue drops the entry.
3. `unit-rerun` runs the `unit` job's own steps once for each lost leg, from the same queue shard plan, and overwrites that leg's durations, flake and coverage artifacts. `shard-check` waits for it, so completeness is graded on the rerun's results.
4. `Gate — Required` accepts a merge group only when `unit` passed with both rerun jobs skipped, or `unit` failed or was cancelled and both `unit-lost` and `unit-rerun` passed. A rerun that fails for any reason, a second runner loss included, fails the gate and the queue drops the entry. Queue retry never requeues it: a run whose `unit rerun` job failed is refused with `failed again on its in-run rerun`.

## Lost fail fast watcher

`queue-fail-fast` holds one `anton-oss-light` runner for the whole merge group Tests run. It polls the run's jobs every 15 seconds until a graded job fails or the run ends, so it is the longest job of the run and the one most exposed to a lost runner. `Gate — Required` grades what the watcher reports, not whether its runner survived: a merge group whose graded jobs all passed stays green when the watcher's runner is lost or the watcher is cancelled.

### What the watcher reports

The `Watch for the first failed graded job` step writes a `failed` step output only when it finds a reason to drop the entry, and the job exports it as its own `failed` output:

| Reason | `failed` value | Then |
|---|---|---|
| A graded job failed, judged by the rule in [Lost unit shard rerun](#lost-unit-shard-rerun) | The failed job's name, for example `unit (3.12, 4)` | `Cancel this queue run so the queue drops the entry` logs `<job> failed, so Gate Required cannot pass; cancelling this queue run.` and cancels the run |
| The queue ref no longer points at the run's commit | `queue group is no longer current (<record>)`, with the [watcher record](#the-watcher-record) | The same cancel step runs |

The watcher ends with an empty `failed` output in every other case:

- every graded job finished without a failure it can prove;
- ten reads of the run's jobs failed in a row, which ends the job green with the warning `Ten reads of this run's jobs failed in a row; the entry waits for Gate Required.`;
- ten reads of the queue ref failed in a row, which ends the job red with `Ten reads of this queue reference failed in a row; queue liveness is unknown.`;
- the runner was lost or the job was cancelled before it wrote an output.

### How Gate Required grades it

On the `merge_group` event the `Require every gate to succeed` step of `Gate — Required` passes only when all of these hold:

1. `queue-fail-fast` is among the job's needs.
2. `select`, `split`, `shard-check` and `test-count` succeeded, and `unit` with `unit-lost` and `unit-rerun` match one of the accepted outcomes in [Lost unit shard rerun](#lost-unit-shard-rerun).
3. `queue-fail-fast` ended `success`, `failure` or `cancelled`, and its `failed` output is empty.
4. Every other need was skipped.

| Watcher result | `failed` output | Graded jobs | Gate |
|---|---|---|---|
| `success` | empty | all passed | passes |
| `failure` or `cancelled` | empty | all passed | passes |
| any | set | any | fails |
| `skipped`, or missing from the needs | any | any | fails |
| any | empty | one did not pass | fails |

A failure the watcher reported always fails the gate, even when the watcher job itself ended `success`. A lost watcher never hides a graded failure: the gate reads the graded jobs' own results. On a pull request the watcher is skipped and the gate requires that. On a dev push or a dispatch the watcher job runs without its watch step and must succeed like every other need.

On a failure the gate prints `Every gate required for this event must succeed and other gates must be skipped.` It does not print the needs it judged on a merge group run, so read each job's result through the API.

### Checking a watcher run

List the watcher and gate jobs of a merge group run:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs?per_page=100" --paginate \
  --jq '.jobs[] | select(.name == "queue-fail-fast" or .name == "Gate — Required") | [.id, .name, .conclusion, .started_at, .completed_at] | @tsv'
```

Read why a red or cancelled watcher ended, and whether it reported a failure:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/check-runs/<queue-fail-fast job id>/annotations" --jq '.[].message'
gh api "repos/the-cloud-clockwork/agentihooks/actions/jobs/<queue-fail-fast job id>/logs" | grep -E 'cannot pass|no longer current|Ten reads'
```

A lost runner shows the annotation `The self-hosted runner lost communication with the server.` and no `cannot pass` line. A `cannot pass` line names the graded job that failed; fix that job.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Watcher `failure` with a lost communication annotation, gate `success`, entry merged | The rule above; the lost watcher was not graded | Nothing to do |
| Watcher `failure` with a lost communication annotation, gate `failure` | A graded job did not pass, or a need that must be skipped ran | List every job of the run and find the graded job that is not `success` |
| Watcher log has `<job> failed, so Gate Required cannot pass` | A graded job failed and the watcher cancelled the run | Fix the named job; the queue dropped the entry |
| `failed` holds `queue group is no longer current` | The group was rebuilt while the run was building | Read the [watcher record](#the-watcher-record) |
| Gate fails on a merge group with the watcher `skipped` | The watcher's `if` condition skipped it, and the merge group gate accepts only `success`, `failure` or `cancelled` | Read the `reuse` job's `reused` output and the watcher's `if` condition in `.github/workflows/test.yml` |

### Limits

- The watcher still holds its runner for the whole run, 27 minutes 5 seconds in the 19 October incident, where it ended 5 minutes 39 seconds after the last graded job finished. This change does not shorten that hold.
- A watcher lost before it saw a graded failure reports nothing, so the run is not cancelled early. The gate still fails on that graded failure when it runs, after the remaining jobs end.

### Delivery evidence

Delivered by pull request [4838](https://github.com/the-cloud-clockwork/agentihooks/pull/4838) for issue [4812](https://github.com/the-cloud-clockwork/agentihooks/issues/4812), after the protected grader admitted the new gate form in [4814](https://github.com/the-cloud-clockwork/agentihooks/pull/4814).

- Incident: merge group run [39209314551](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39209314551) for pull request 4640 on 19 October. Every graded job passed; `queue-fail-fast` ran from 06:20:26 to 06:47:31 UTC, lost communication with its runner and ended `failure`, and `Gate — Required` failed, so the queue dropped the entry.
- Red: run [39267799616](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39267799616) failed five regression cases on the workflow before the gate changed.
- Green: run [39268178025](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39268178025) passed 613 tests, including the workflow gate cases.
- The regression tests in `tests/test_ci_reuse.py` run the gate step from `.github/workflows/test.yml` for every watcher result (`success`, `failure`, `cancelled`, missing), every `failed` value (empty, a unit shard, a rebuilt group) and every graded `unit` result. The gate passes only for a present watcher, an empty `failed` and a passed `unit`. A second test pins the job's `failed` output to the watch step.
- Queue groups dropped only for a lost watcher runner, counted over merge group Tests runs in the 24 hours on each side of the merge at 19:52:58 UTC on 19 October: 1 of 274 runs before (run 39209314551) and 0 of 335 runs after. Every other red or cancelled watcher in both windows sat in a run whose conclusion was `cancelled`. Count it again for any window:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs?event=merge_group&created=<start>..<end>&per_page=100" --paginate \
  --jq '.workflow_runs[] | select(.conclusion == "failure") | [.id, .run_attempt] | @tsv' |
while read -r id attempt; do
  gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$id/attempts/$attempt/jobs?per_page=100" --paginate \
    --jq '.jobs[] | select(.name == "queue-fail-fast" and .conclusion == "failure") | .run_id'
done
```

Each printed run had a failed watcher in a failed run; read its annotations as in [Checking a run](#checking-a-watcher-run) to confirm a lost runner.

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
   - `The job log could not be read`

   The last is recorded when the job log API refuses the read, as it does for a shard lost before it uploaded a log, unless an annotation shows a step exited nonzero. One red shard beside a lost one refuses the requeue.
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

#### Recovery timestamps

Recovery records include the following ISO 8601 UTC times, delivered by [pull request 5389](https://github.com/the-cloud-clockwork/agentihooks/pull/5389). Recording needs no extra setting and applies to both the scheduled workflow and the tick's cluster watchdog.

| Field | Meaning |
|---|---|
| `run`, `attempt` | GitHub run ID and the attempt examined; keep both when comparing recoveries. |
| `detected_at` | Time the second assessment confirms the stall, before the run attempt is checked again. It is not the first suspicion or the time the runner disappeared. |
| `cancel_requested_at` | Time immediately before the plain cancel API request. It records an attempted request, including one GitHub refuses. |
| `force_cancel_requested_at` | Time immediately before the force cancel API request; `null` when that request was not attempted. |
| `completed_observed_at` | Time of the first poll, or follow-up read after a cancel API error, that reports the run `completed`; `null` when completion was not observed. |

For example, this illustrative recovery attempted both cancellation methods and then reran failed jobs:

```json
{"run":123456,"attempt":1,"action":"rerun","reason":"lost runner; first attempt without an independent job failure","detected_at":"2026-10-20T12:00:00+00:00","cancel_requested_at":"2026-10-20T12:00:01+00:00","force_cancel_requested_at":"2026-10-20T12:03:01+00:00","completed_observed_at":"2026-10-20T12:03:11+00:00"}
```

After each cancellation request, the watchdog sleeps ten seconds before each of up to eighteen status reads. If plain cancel ends the run, force cancel is skipped. Otherwise force cancel gets the same polling budget. API time adds to these waits. The completion observation can therefore lag GitHub's state change by the polling interval and API time. GitHub's workflow run REST response supplies no `completed_at`; `updated_at` is not a completion time.

Read retained tick recovery records without triggering cancellation:

```bash
jq -c 'select(.detected_at != null) |
  {run, attempt, action, reason, detected_at, cancel_requested_at,
   force_cancel_requested_at, completed_observed_at}' \
  "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/lost-runner.log"
```

For the scheduled workflow, read the JSON lines in the **Cancel stalled Tests runs and rerun their failed jobs once** step of the Lost runner run. The tick writes its output to the log above; the workflow writes to its job log.

To calculate observed seconds after each request, pipe the selected JSON records above into this command. It accepts fractional seconds as well as the example's whole seconds:

```bash
python3 -c '
import json
import sys
from datetime import datetime

for line in sys.stdin:
    row = json.loads(line)
    end = row.get("completed_observed_at")
    if not end:
        continue
    elapsed = {
        field: (datetime.fromisoformat(end) - datetime.fromisoformat(row[field])).total_seconds()
        for field in ("detected_at", "cancel_requested_at", "force_cancel_requested_at")
        if row.get(field)
    }
    print(json.dumps({"run": row["run"], "attempt": row["attempt"], "seconds_since": elapsed}))
'
```

The example gives 191 seconds since detection, 190 since plain cancel and 10 since force cancel. A record with both requests includes the plain cancel wait before force cancel; it cannot prove how fast force cancel alone would have ended the run. Compare complete records over a named window, retain records without completion as unmeasured, and keep error outcomes separate.

#### Running and configuring recovery

The timestamp fields need no environment flag. To run recovery manually from a checkout with dependencies installed, use an authenticated `gh` with Actions write access and organization self-hosted runner read access. `GH_TOKEN` can supply the token; keep its value outside commands and logs. This command can cancel the selected run and rerun its failed jobs under the policy above:

```bash
python -m scripts.ci_lost_runner \
  --repo The-Cloud-Clockwork/agentihooks \
  --workflow test.yml --run-id 123456 --grace-minutes 10
```

Replace the example run ID with the intended run. Repeat `--run-id` to select several runs; omit it to examine all unfinished runs of `--workflow` (default `test.yml`). The workflow file is read from the checkout to resolve job dependencies. `--grace-minutes` sets the minimum job age, default ten minutes, or zero with `--cluster`. Cluster mode also switches to the cluster evidence and thirty second confirmation described above. `--lock <path>` optionally holds a file lock for the invocation; a held lock prints `action: none` and exits successfully. Cancellation polling has no CLI override.

| Observation | Interpretation and action |
|---|---|
| Force cancel time is `null`, completion is present | The run completed before force cancel was attempted. Read `action` and `reason` to distinguish a rerun, cancellation or a run that completed meanwhile. |
| Completion is `null`, reason says the run did not end after force cancel | Both polling budgets expired without observing completion. Read the run's current status before treating it as freed. |
| `action: error` with timestamps | Recovery began and an API operation failed. Attempted cancel times and any observed completion survive the error, including a failed rerun request. Read `reason`; a request timestamp does not prove GitHub accepted it. |
| No timing fields | Older records and decisions made before recovery, including early read errors, have no recovery timings. A run rerun meanwhile is also left alone without these fields. Do not fill gaps with zero or `updated_at`. |

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

## Merge queue churn measurement

On 20 October one pull request took 109 minutes from enqueue to merge while its merge group was cancelled and rebuilt six times, and 17 of the last 30 finished merge group Tests runs had been cancelled with none failed. A read only measurement over one day of `dev` merge groups counted every cancelled run, named what cancelled it and summed what it cost ([issue 5401](https://github.com/the-cloud-clockwork/agentihooks/issues/5401), ledger task t584). It changed nothing; the fixes it led to are listed below.

### Window and queue settings

The window ran from 2026-10-20T02:00Z to 2026-10-21T02:00Z. It held 301 merge group Tests runs: 170 `success`, 126 `cancelled`, 5 still queued and none `failure`. The `dev` merge queue then built up to five entries at once (`max_entries_to_build` 5), merged up to five, grouped `ALLGREEN`, waited one minute for entries and timed checks out after 45 minutes. `.github/rulesets/dev-no-delete.json` now sets `max_entries_to_build` to 1; the other values are unchanged.

### How each cancelled run was attributed

Every cancelled run falls into one of two kinds:

- **Ejection of the entry itself.** A job of the run itself concluded `failure`, `timed_out` or `startup_failure`, and the run's `queue-fail-fast` job cancelled it; or the queue timed the entry out (`checks_timed_out`). This split is read from job conclusions, not from removal reasons: GitHub can give `failed_checks` even when every own run was cancelled clean. The fail fast job logs the failed job before cancelling: `<job> failed, so Gate Required cannot pass; cancelling this queue run.`
- **Rebuild caused by another entry.** No job of the run failed; GitHub rebuilt the group because an entry ahead of it left the queue, and cancelled the run building the old head. The trigger is the removal event of the entry ahead whose time falls inside the run: `failed_checks`, a removal by hand or a priority move (`manual`), or `checks_timed_out`.

A cancelled run with no failed job and no removal ahead inside its window stays unattributed: an enqueue ahead, a base push or a ruleset change leaves no queue event that names it. Wall minutes are the run's own duration; runner minutes are the sum of its jobs' durations.

The [queue rebuild records](#queue-rebuild-records) now name the cause of each rebuild cancellation directly in the watcher record; the removal ahead cause arrived with pull request 5706 on 22 October, after this measurement, which attributed rebuilds by hand from the same queue events.

### Counts and costs

| Kind | Trigger | Runs | Wall minutes | Runner minutes | Example runs |
|---|---|---|---|---|---|
| Ejection of the entry | A job of its own failed | 36 | 430 | 3424 | [39506054106](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39506054106) |
| Ejection of the entry | Checks timed out | 1 | 48 | 133 | [39506046726](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39506046726) |
| Rebuild by another entry | Entry ahead failed checks | 38 | 298 | 1627 | [39505435816](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39505435816) |
| Rebuild by another entry | Entry ahead removed by hand or moved by priority | 16 | 86 | 124 | [39507217466](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39507217466) |
| Rebuild by another entry | Entry ahead timed out | 4 | 102 | 216 | [39510499547](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39510499547) |
| Rebuild by another entry | No queue event names it | 31 | 349 | 2344 | [39519081840](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39519081840) |
| Total | | 126 | 1311 | 7867 | |

Row minutes are rounded per run, so they can differ from the totals by a minute. Ejections of the entry itself came to 37 runs and 3557 runner minutes; rebuilds caused by another entry came to 89 runs and 4310 runner minutes.

The leading trigger was an entry failing in the queue. Its own ejections plus the rebuilds it caused behind it came to 74 runs and 5051 of the 7867 cancelled runner minutes. Removals by hand were frequent, 125 in the window and every one queued again (median 38 seconds later), but cost little: their 16 attributed rebuilds came to 124 runner minutes.

Passing groups in a sample of 100 successful runs took 14.8 minutes median and 40.0 at most.

### Rebuilds per merged pull request

| Rebuilds before merging | Pull requests |
|---|---|
| 0 | 111 |
| 1 | 25 |
| 2 | 14 |
| 3 | 7 |
| 4 | 3 |
| 5 | 1 |

Across the 161 merged pull requests, enqueue to merge took 88.8 minutes median and 185.6 at the 90th percentile: 77.7 minutes median with no rebuild and 102.5 with one or more. Builds discarded on pull requests that later merged came to 864 wall minutes.

### Why entries kept failing

Of the 36 own job failures, 15 failed in `Run tests` and 11 in `Install dependencies`. Most test failures were deterministic. Counted per test, so one run can appear under more than one test: the code owner test failed nine times on one pull request, the CI script inventory test three times and the ledger cutover test four times. The install failures were PyPI answering 502 on `setuptools` between 21:34Z and 22:06Z, which `uv` gave up on within eight seconds.

The queue timelines, a separate count from the run table above, recorded 20 removals with reason `failed_checks`; 19 of them were queued again at an unchanged head. Pull request 5129 entered at head `527d01a7` and was queued again six times with no new commit. Eight merge group runs at that head failed the same code owner test after those requeues: runs [39396055512](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39396055512), [39404264587](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39404264587), [39408630475](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39408630475), [39412887866](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39412887866), [39413818395](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39413818395), [39427751037](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39427751037), [39436310682](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39436310682) and [39448639696](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39448639696). These re-failures cost 809 runner minutes, and the 10 rebuilds they caused behind them another 216.

The cause was in the swarm's queue exit judgment. `queue-fail-fast` cancels its own run on the first failed job, so a failing entry's run ends `cancelled`, never `failure`. The merge pass counted a `cancelled` run as clean, read the exit as having no failure of its own and queued the same head again.

### Fixes it led to

| Fix | Pull request |
|---|---|
| A cancelled merge group run with a failed, timed out or startup failed job counts as the entry's own failure, and `agentihooks swarm <slug> merge queue <url>` refuses a head whose last merge group run failed until a new commit lands; see [Ready merge pass queue exits](../pillars/swarm.md#ready-merge-pass-queue-exits) | [5466](https://github.com/the-cloud-clockwork/agentihooks/pull/5466) |
| The code owner and script inventory tests run on pull requests that add, remove or move files, so they fail before the queue | [5470](https://github.com/the-cloud-clockwork/agentihooks/pull/5470) |
| The unit dependency install retries through a short PyPI outage | [5633](https://github.com/the-cloud-clockwork/agentihooks/pull/5633) |
| The mutation and test planner dependency installs retry the same way | [5665](https://github.com/the-cloud-clockwork/agentihooks/pull/5665) |

### Repeat the measurement

All reads go through `gh` with read access to the repository. List the window's merge group Tests runs with their conclusion and wall minutes:

```bash
gh api --paginate "repos/<owner>/<repo>/actions/runs?event=merge_group&created=<from>..<to>&per_page=100" \
  --jq '.workflow_runs[] | select(.name == "Tests")
        | [.id, .conclusion, .head_branch,
           (((.updated_at | fromdate) - (.run_started_at | fromdate)) / 60 | floor)] | @tsv'
```

`<from>` and `<to>` are UTC times such as `2026-10-20T02:00:00Z`. The branch `gh-readonly-queue/dev/pr-<number>-<sha>` names the entry. For each cancelled run, sum its runner minutes and list any failed job:

```bash
gh api --paginate "repos/<owner>/<repo>/actions/runs/<run id>/jobs?per_page=100" \
  --jq '.jobs[] | select(.started_at and .completed_at)
        | [.name, .conclusion, (((.completed_at | fromdate) - (.started_at | fromdate)) / 60)] | @tsv'
```

A job concluded `failure`, `timed_out` or `startup_failure` makes the run an ejection of the entry itself. Otherwise read the queue removals of the entries that were ahead of it, and keep the ones inside the run's window:

```bash
gh api graphql -f query='query($url: URI!) { resource(url: $url) { ... on PullRequest {
  timelineItems(last: 100, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) { nodes {
    __typename
    ... on AddedToMergeQueueEvent { createdAt actor { login } }
    ... on RemovedFromMergeQueueEvent { createdAt actor { login } reason }
  } } } } }' -f url="https://github.com/<owner>/<repo>/pull/<number>"
```

An added event after a removal with no commit between them is a requeue at an unchanged head.

### Measurement limits

- The measurement covers one day and one repository, before the fixes above merged; it has not been repeated after them.
- 31 of the 126 cancelled runs could not be attributed from queue events.
- The pass sample for passing group duration is 100 of the 170 successful runs.

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
| `<n> lines the base ran are no longer run by any head test; see the coverage-ratchet report.` | A merged change removed or rerouted the only test reaching those lines, listed by file and line in `report.txt`, so promotion is red while the queue keeps merging. | Follow [Clearing a lost line](#clearing-a-lost-line); never lower the ratchet or exclude the line. |
| A coverage job ran in a merge group and `Gate — Required` failed with `Every gate required for this event must succeed and other gates must be skipped.` | A workflow change gave a coverage job a `merge_group` condition. | Restore the event conditions in the table above. |

### Clearing a lost line

The ratchet grades a base line only through its place in the head file. `tests/coverage_grade.py` maps each line the base ran onto the head source with a line diff. A renamed file is mapped line by line, and a file whose definitions moved into several files is mapped definition by definition. A base line with no unchanged match in the head, because the change deleted or rewrote it or deleted its file, is not graded. A base line that still exists in the head is graded whether its callers changed or not.

Read `report.txt` first. Each lost line has its own line:

```text
scripts/swarm/ledger_probe.py:42 ran on base dff4aa25a502d1b98bb625828cc77c96d47ec8a1 and no head test runs it
```

```bash
gh run download <run id> --repo the-cloud-clockwork/agentihooks \
  --name coverage-ratchet --dir coverage-ratchet
grep "no head test runs it" coverage-ratchet/report.txt
```

Then decide per line:

| What the line is now | Fix |
|---|---|
| Code the change left without callers | Delete it. Confirm first that nothing calls it, for example `git grep "\.took()" origin/dev -- scripts tests hooks` returns nothing. The deleted line drops out of grading. |
| Code still reached in production, which the base's tests only reached indirectly | Add a unit test that calls it directly and asserts what it does. |
| Code the change's own tests stopped reaching | Restore or add the test that reaches it. |

Every fix goes through a normal pull request into `dev`. The promotion fixer task is closed by the next green dev push run, which passes `coverage-ratchet`.

#### The 18 October ledger probe loss

Pull request [4266](https://github.com/the-cloud-clockwork/agentihooks/pull/4266) moved the ledger probe notices onto the retained pass summaries. Its dev push, commit `740ce511`, failed `coverage-ratchet` in [run 39091332968](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39091332968), graded against base `dff4aa25`. `report.txt` listed three lines:

- `scripts/swarm/ledger_probe.py:42` and `:43`, the body of `Sample.took`. Pull request 4266 had replaced every caller, so the method was left with none.
- `scripts/swarm_ledger/ledger_server.py:875`, the `return None` in `Handler.put_swarm` when `refused(slug)` is true. Neither the server nor its tests changed in that range; the base reached the line only indirectly.

Pull request [4374](https://github.com/the-cloud-clockwork/agentihooks/pull/4374), merged on 18 October 2026, applied the first two rows of the table above:

- It deleted `Sample.took`.
- It added `test_the_swarm_route_stops_at_a_refused_request` in `tests/test_swarm_lifecycle.py`. The test calls `Handler.put_swarm` with `refused` returning true and asserts it returns `None`, sends no reply and never calls `lifecycle_control`.

The dev push of its merge, commit `75b9649d`, in [run 39099281007](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39099281007), no longer listed any of the three lines. That run still failed the ratchet on one other line, `scripts/inbox/store.py:714`, which pull request 4374 did not touch. The dev push run on commit `96fa76fe`, [run 39101112212](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39101112212), passed `coverage-ratchet` against the same base.
