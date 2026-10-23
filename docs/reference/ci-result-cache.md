---
title: CI result cache and in place retry
parent: Reference
nav_order: 5
---

# CI result cache and in place retry

One CI algorithm for every repo and workflow: a check whose real inputs did not change reuses its last green result, and a check that fails is retried in place a bounded number of times inside the same job. No run is ever restarted from zero because one test flaked.

Status: slices 1 to 6 delivered (see [Slice 1 as delivered](#slice-1-as-delivered), [Slice 2 as delivered](#slice-2-as-delivered), [Slice 3 as delivered](#slice-3-as-delivered), [Slice 4 as delivered](#slice-4-as-delivered), [Slice 5 as delivered](#slice-5-as-delivered) and [Slice 6 as delivered](#slice-6-as-delivered)), with full scope pull request reuse added to slice 5 (see [Full scope pull request reuse as delivered](#full-scope-pull-request-reuse-as-delivered)); slice 7 is design. Issue [#2365](https://github.com/the-cloud-clockwork/agentihooks/issues/2365). Slices are listed in [Slices](#slices); each one is a separate pull request.

## Before numbers

Measured on the Tests workflow from 2026-10-04 to 2026-10-11 with the Actions API. Raw runs, job logs and the classification scripts sit in the task work folder; every count below is tied to run ids there.

| Event | Runs | Failed | Failures caused by a flake | Wall time of those failed runs |
|---|---|---|---|---|
| Merge queue | 544 | 86 | 32 test flakes, 10 artifact download races | 194 min test flakes, 120 min download races |
| Pull request | 3421 | 634 | 97 of 124 reruns ended green on the same head | up to 3771 min, span including human delay |
| Dev push | 1135 | 125 | 4 of 10 reruns ended green on the same head | up to 62 min, same caveat |

All 86 failed queue runs, 707 minutes:

| Cause | Runs | Minutes |
|---|---|---|
| A test failed, then the same pull request passed the queue with no new commit | 32 | 194 |
| A gate job failed: Sonar 10, coverage ratchet 10, queue baseline 10, size 2, swarm image 3 | 35 | 328 |
| The durations download in the unit job lost a race with a newer queue entry | 10 | 120 |
| A real regression on dev: a missing code owner entry broke 5 queued pull requests | 5 | 37 |
| Unresolved: 2 unit timeouts, 2 with no failing test in the sampled log | 4 | 28 |

The restart is the cost. On a failed queue run, the job that failed took on average 41.9 % of the run's wall time (median 39.5 %); the other 58 % was every passing job running again. A failed queue run also sends the pull request back to the end of the queue.

Most failing tests in the queue: the inbox notify test with a 2.5 second budget (7 runs, all in one 10 minute burst of runner contention), three fakeredis ledger tests (3 runs each), and the two canaries below.

### The canaries

| Test | Queue run that failed | Pull request run that passed | Same tree | Failure |
|---|---|---|---|---|
| Header counts: `test_section_heads.py::test_every_header_shows_its_counts_folded_and_open` | 37949214650 (#2192), 37999229992 (#2254) | 37948373213, 37998333873 | same pull request commits, dev moved | every count read back empty: the page's count fetch had not resolved |
| Alerts fold reload: `test_alerts_page.py::test_the_panel_fold_is_remembered_after_a_reload` | 37957410684 (#2196) | 37956518207 | identical tree | `assert True is False`: the remembered fold state raced the reload |

Each cost 5 m 16 s to 5 m 49 s of queue wall time plus a full replacement queue run of about 6 minutes. #2254 then failed the queue twice more on gate jobs and spent 35 m 46 s in the queue in total.

These two tests stay unfixed until slice 1 lands, so they keep proving the retry.

## What exists today

| Mechanism | Unit | Key | Gap |
|---|---|---|---|
| Merge queue reuse of a passed tree: the `reuse` job, `scripts/ci_reuse.py` | whole Tests run | head tree, base tree, grader tree, `.github` tree, UTC day | all or nothing; any dev move between pull request and queue misses |
| Cached shard environments: `venv-*`, `chromium-*`, `sonar-*` caches | environment | interpreter, `pyproject.toml`, day; Playwright version | caches tools, not results |
| Durations and coverage baselines: `durations-merged-<sha>`, `coverage-baseline-<tree>` | data | commit or tree | not results |
| Mutation reuse of the preflight result: `own_bases` and `graded_green` in `scripts/ci_mutation/scope.py` | mutation scope | ancestor commit whose preflight `mutation` job passed | per commit, not per input |
| Helm kind chart proof: `kind-due` and `helm-kind` | one job | job input key | none: `kind-due` looks up the `helm-kind` key |
| Dev push | whole run | none | always runs everything; `ci_reuse.py` decides reuse only for `merge_group`, so the dev push reuse named in the task does not exist yet |

Before slice 1 nothing retried. A failed test failed its shard, the shard failed `Gate — Required`, and the only recovery was a rerun of failed jobs on a pull request or a new queue entry.

## The algorithm

Every check, whether a test or a job, is a unit with an id and an input key.

1. **Key.** `key = H(unit id, environment key, input fingerprint)`. The environment key covers the interpreter, the resolved dependency set, tool versions such as Playwright and Chromium, and the runner image. The input fingerprint is computed by an adapter from the unit's real inputs.
2. **Plan.** Before running, look up each key in the result store. A green record with the same key, younger than the record lifetime, written by a trusted writer, is reused: the unit is not run and is reported as reused, naming the run that produced the record.
3. **Run with retry in place.** Every unit that is not reused runs. A failure is retried in the same process or job, up to two more attempts. A pass after a retry is green and is written to the flake ledger with the unit id, the attempt count and each failure line. A failure on every attempt is red.
4. **Record.** Each job uploads its flake ledger and its results. Only trusted writers turn results into store records.
5. **Grade.** `Gate — Required` reads the counts (ran, reused, flaky, failed) from the result files. A missing or unreadable result file is red, as is any gate that cannot run.

### Retry rules

- Bounded: three attempts in total, set once per repo. A consistent failure stays red. Proven: the workflow shape test failed all three attempts in run 38099973312 and its shard stayed red.
- In place: the retry runs inside the same pytest worker with function fixtures rebuilt and wider fixtures kept. A broken session fixture, such as a dead browser, fails again and stays red.
- Counted: every retried test is one ledger line with its attempts and each failure line. A unit flaky on two separate runs within seven days raises a ledger follow up naming it. Retry absorbs a flake; it does not remove the follow up to fix it.
- Off where kills matter: mutation runs and the leak detector run without retry, because a retried test can let a mutant survive or hide an order dependent leak. The retry flags go on the pytest command line of the unit step, never in `PYTEST_ADDOPTS`, so nested runners do not inherit them.
- Jobs: a job step retries in place only when its workflow declares it idempotent, such as an artifact download, a registry pull or a Sonar server call. Deploys never retry. The durations download race (10 queue failures) is the first such step.

### Input fingerprints for tests

A static import closure does not work in this repo. Over the last 40 dev merges, a test file counted as unchanged when nothing it imports, directly or transitively, nor its conftest chain changed. Averaged per merge, the reusable share was 34.9 % counting imports only and 29.9 % also counting changed data files, median 0; a merge that changed `.github`, `pyproject.toml` or the stored durations counts as nothing reusable (6 of 40). The root `tests/conftest.py` imports the installer and the swarm modules, so most merges reach every file through it.

The pytest adapter therefore fingerprints what a test actually executed:

- **Executed code.** The 3.12 shards already measure coverage with the sysmon core. With per test contexts on, each test gets the set of functions it executed. Its fingerprint is the hash of those functions' source plus the module level code of every file it touched. A change to a function the test never ran does not invalidate it. This is the pytest-testmon algorithm.
- **Files read.** An audit hook records every repository file the test opens in process. Each is hashed whole.
- **Escapes.** A test that starts a subprocess or drives a browser cannot be traced into the child. Its fingerprint is the hash of every source tree the child can load: `hooks/`, `scripts/` and the page assets for browser tests. Such a test is reused only when all of those are unchanged.
- **Hidden inputs.** Wall clock, randomness and network are not inputs. Records expire after seven days, and dev runs every test again on every push, so a hidden dependency shows up within one dev push.

Other languages plug in an adapter with the same contract: list units, compute a fingerprint per unit, run a chosen set with retry, write one result line per unit.

### The store

- **Writers.** Only runs of protected code write records. The save step sits in a job that needs `Gate — Required` to succeed and runs only when `github.event_name == 'push' && github.ref == 'refs/heads/dev'`, or on the scheduled full run on dev. GitHub scopes every cache entry to the ref that saved it, so an entry a pull request or queue run saves is visible only to later runs of that same ref and never to dev or to other pull requests. The planner runs from the protected grader checkout, as the `reuse` job does, and accepts a record only when its `run` field names a successful dev push or scheduled run of the Tests workflow, checked with one API call. A pull request can at most mislead its own runs, which already run its own code.
- **Readers.** Pull request runs read the newest dev record through the Actions cache, which a `pull_request` run can restore from its base branch scope without Actions API quota. Queue runs execute on a `gh-readonly-queue/dev/...` ref, and GitHub does not document that ref restoring from dev's scope; the default branch here is `main`. Queue runs therefore fetch the record as an artifact of the newest dev push run with the App token, the path the `reuse` job already uses. Slice 4 starts with a planted cache read on a real queue run and moves queue runs to the cache only if it resolves. Each record holds the dependency manifest of every test: functions, files, environment key.
- **Queue.** The queue runs every test whose fingerprint differs from the dev record, which always includes the pull request's own tests. The whole tree reuse in `ci_reuse.py` stays as the faster path when the tree is identical.
- **Dev.** A dev push runs everything and writes the next record. It is the freshness backstop for the cache and the source of truth for coverage.

### Coverage and Sonar

Reused tests produce no coverage, while the coverage ratchet and Sonar need the full suite. The dev record keeps each test's coverage context. A partial run combines its own coverage with the dev record's contexts for the reused tests, which stay valid because their inputs did not change. Until slice 4 lands, any run that reuses tests also runs the full suite for coverage, so reuse first ships for jobs that do not feed coverage.

## Slices

Each slice is one pull request with its own before and after number and a both ways proof.

| # | Slice | Anchor | Number it moves | Proof |
|---|---|---|---|---|
| 1 | In place test retry and flake ledger | the `unit` job's Run tests step; a pytest plugin; `pytest-rerunfailures`; a Record flaky tests step and a `flakes-<python>-<shard>` artifact; flake counts in `Gate — Required` | queue runs failed by a test flake: 32 a week, 194 min | planted first attempt failure on both canaries absorbed with the shard green; a consistent failure stays red after three attempts |
| 2 | Idempotent step retry | a `retry` wrapper for declared steps; first user is the durations download in `split` and `unit` | queue runs failed by the download race: 10 a week, 120 min | planted first attempt download failure absorbed; a missing artifact still red |
| 3 | Flake budget | dev push gathers the flake artifacts into a cached history; a follow up is raised for any test flaky twice in seven days | flaky test ids a week, today unknown | a planted repeat flake raises one follow up |
| 4 | Test dependency manifests | per test coverage contexts on the 3.12 shards; a manifest builder after the coverage merge in `sonar`; the manifest saved to the Actions cache on dev pushes | the measured reusable share over the last 40 merges with real manifests, against 30 to 35 % for static imports | a planted change to one executed function invalidates exactly the tests that ran it |
| 5 | Planner | collection time deselection of reused tests in pull request and queue runs; shards split on the remaining tests; reused, ran and flaky counts in the gate summary; coverage combined from the dev record | pull request and queue unit wall time; queue median today 8 m 23 s | a planted change re-runs its tests and reuses the rest; a missing record falls back to a full run |
| 6 | Job input keys | generalize `ci_reuse.py` and `kind-due`: each job declares its input paths and tools, and a passed key skips its heavy steps at step scope | wall time of jobs whose inputs did not change | a planted change to a declared input runs the job |
| 7 | Fleet adoption | the plugin, the retry wrapper and the key action move to the organization's reusable workflows; other repos adopt per Delivery Requirements | repos on the shared mechanism | each repo's own both ways proof |

Slice 1 removes the restarts behind the canaries. Slices 4 and 5 are the result cache and depend on the coverage combine. Slice 6 brings the same rule to jobs that are not tests.

## Proof that retry absorbs the canaries

Dispatched Tests runs on a diffcheck branch carrying slice 1's plugin with `--reruns 2`, the canaries set to fail their first attempt:

| Run | Plant | Result |
|---|---|---|
| [38099973312](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38099973312) | none on purpose: the workflow shape test fails every attempt because the proof adds a step | three attempts, recorded `failed`, shard red: a consistent failure is not masked |
| [38100556344](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38100556344) | header counts and alerts fold reload (both parameters) fail attempt 1 | each passed on attempt 2 and was recorded `flaky`; all 12 unit shards and both shard checks green; run attempt 1, no rerun. The run concluded failure only on `lint` and `test-count`, see below |

The ledger lines from run 38100556344:

```json
{"test": "tests/swarm_ledger/test_section_heads.py::test_every_header_shows_its_counts_folded_and_open", "attempts": 2, "outcome": "flaky", "errors": ["E   AssertionError: planted failure on attempt 1"], "worker": "gw2"}
{"test": "tests/swarm_ledger/test_alerts_page.py::test_the_panel_fold_is_remembered_after_a_reload[0]", "attempts": 2, "outcome": "flaky", "errors": ["E   AssertionError: planted failure on attempt 1"], "worker": "gw3"}
{"test": "tests/swarm_ledger/test_alerts_page.py::test_the_panel_fold_is_remembered_after_a_reload[250]", "attempts": 2, "outcome": "flaky", "errors": ["E   AssertionError: planted failure on attempt 1"], "worker": "gw0"}
```

The plant fails the test's first call before its body runs, so it proves the retry path in CI on the canaries' real fixtures (Chromium, the scratch ledger server). The canaries' real failures are timing races inside one attempt; that a second attempt clears them is a hypothesis until slice 1 runs in the queue, where the flake ledger records each absorbed failure. The proof branch's lint and test count jobs were red because the branch sat behind the dev floor they grade against, which retry does not touch.

## Slice 1 as delivered

The unit job's Run tests command line carries `-p tests.flake_ledger --reruns 2`. `tests.flake_ledger` runs on the xdist controller and writes one line per retried test to `CI_FLAKES`, after the test's last attempt. Each shard uploads it as `flakes-<python>-<shard>`, prints outcome, attempts and test id to its step summary, and raises a `Flaky test` warning annotation on the run and the pull request for every test a retry absorbed, so no flake passes unseen. Flake counts in `Gate — Required` move to slice 3: the gate's steps are generated by `ci_reuse.gate_steps`, and slice 3 already gathers the flake artifacts.

| Run | Plant on the delivered code | Result |
|---|---|---|
| [38103409208](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38103409208) | one test fails its first attempt | all 12 unit shards green on run attempt 1; `flakes-3.11-1` and `flakes-3.12-5` record it `flaky`, 2 attempts, run 38103409208 |
| [38103411818](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38103411818) | one test fails every attempt | exactly the two shards that ran it failed, `unit (3.11, 4)` and `unit (3.12, 8)`; each records it `failed`, 3 attempts, run 38103411818; the other 10 shards green |

Cost to the phase: a retry spends time only on a failing test, at most two more runs of it. The pull request run of this slice (38103509370) took 160 to 276 s per unit shard against the 7 minute job timeout, with an empty flake report on every shard. A flake absorbed in the queue saves the replacement queue run, about 6 minutes each, 32 of them last week.

## Slice 2 as delivered

`.github/actions/retry-download` downloads one artifact of the run with `actions/download-artifact`, up to three attempts. Attempts 1 and 2 are `continue-on-error`; each failure raises a `Retried download` warning annotation and waits 10 s, then 20 s. Attempt 3 is not, so an artifact that never appears still fails the step. Using the action is the declaration that a step is idempotent. `split` downloads the queued run's dev durations through it and `unit` downloads the durations it splits on. `tests/test_ci_step_retry.py` fails when a job holding a deployment environment or an OIDC token uses it, directly, through a local composite action or through a called workflow.

The 10 queue failures counted above all ran on 2026-10-08 in the unit job's older durations lookup, which called the GitHub API and ran out of the shared token quota; the cache restore that replaced it had no repeat. The retry covers what remains: one transient artifact service error no longer fails a shard and its queue entry.

| Run | Plant on the delivered code | Result |
|---|---|---|
| [38108213778](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38108213778) | attempt 1 asks for a name that does not exist | 2 of 2 split and 12 of 12 unit download steps green on attempt 2, each with one `Retried download` warning; the two shards running `test_ci_step_retry` went red on the planted action file, as that test should |
| [38108215538](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38108215538) | `unit` asks for a durations artifact that does not exist | 12 of 12 unit shards red in the download step after three `Artifact not found` attempts and two warnings; Run tests skipped |

Cost: nothing when the first attempt succeeds; 30 s plus the attempts when an artifact is missing for good.

## Slice 3 as delivered

The `flake-budget` job runs on every dev push after `unit`. It restores the newest `flake-history-` cache, downloads the run's `flakes-3.*` reports and runs `python -m tests.flake_budget`. That keeps one line per test and run for every `flaky` outcome, folds parameters into the test function, drops lines older than seven days, and writes `flake-budget.json`: the count of flaky test ids in the window, the number this slice moves, every test flaky in two or more separate runs (`over_budget`), and every test with any retry pass in the window with its exact run count and runs (`retried`). The job prints the count and each retried test to its step summary, raises a `Flake budget` warning per test over budget, uploads the `flake-budget` artifact and saves the history under `flake-history-<run>-<attempt>`. A job that cannot count or publish is red.

CI runners cannot reach the ledger, so the follow up is filed by the swarm's minute tick, beside the CI budget defects pass. `scripts.ci_budget.flakes.refresh` reads the newest dev `flake-budget` artifact every five minutes and adds one follow up per test over budget, in plain words naming the test, its folder and the number of runs. A test that has been raised is not raised again for seven days; a test still flaky twice in a later window gets a new follow up. A ledger that cannot be reached skips that test until the next pass.

| Run | Plant: one test fails its first attempt | Result |
|---|---|---|
| [38122887391](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38122887391) | first run on a fresh history | no cache found; 1 flaky test id; nothing over budget; the tick pass filed nothing |
| [38123717172](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38123717172) | second run | history restored from the first run; the planted test over budget with both run ids; the tick pass filed exactly one follow up on a scratch ledger |
| [38124767888](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38124767888) | third run | the planted test listed with three runs; the tick pass filed nothing new; the ledger still held one follow up |

The proof branch ran the job on dispatch instead of push; everything else is the delivered code.

Known limits: two dev pushes whose `flake-budget` jobs overlap restore the same history, and the later save drops the earlier run's lines, so the count can undercount. A job concurrency group would instead cancel a pending dev push run, so it is not used. A follow up whose write lands but whose reply times out is filed again on the next pass.

## Slice 4 as delivered

The 3.12 unit shards run with `--cov-context=test` and `-p tests.dep_inputs`. The plugin's audit hook records every repository file opened for reading (Python sources excluded; coverage covers them) and whether a test starts a process (`subprocess.Popen`, `os.posix_spawn`, `os.spawn`, `os.system`, `os.exec`, `os.fork`). A read made outside any test, during collection for instance, counts for every test of that worker. Each shard ships its `dep-inputs/` lines in its `coverage-3.12-<shard>` artifact.

After the coverage merge, `sonar` runs `python -m tests.dep_manifest build` on full scope runs and uploads `dep-manifest`; a build with no input reports is red. Each executed line maps to the innermost function whose body holds it; every other line, def and decorator lines included, is the file's module level unit, and every test that ran a line of a file also depends on that file's module level unit. A function's fingerprint is its source from the def line to its end, so a nested function's change also moves its parent. The module level fingerprint skips blank lines and comment tokens, so adding a function moves nothing a test ran. `coverage-baseline`, which runs only on dev pushes, saves the run's manifest to the Actions cache as `dep-manifest-<sha>` once `Gate — Required` passed; the sonar job itself never names the dev head.

`python -m tests.dep_manifest invalidated` lists the tests a diff invalidates, from the manifest's commit to a head:

- a change under `.github/`, to `pyproject.toml`, `uv.lock` or the stored durations, or to Python outside `hooks/`, `scripts/` and `tests/` invalidates every test;
- a changed function or module level unit under `hooks/` or `scripts/` invalidates the tests that ran it;
- a test module invalidates its own tests; any other Python file under `tests/` invalidates every test;
- any other file invalidates the tests that opened it;
- any change under `hooks/`, `scripts/` or `tests/` invalidates every test that started a process.

A test that only reads a module constant runs no line of that module, so no context names the dependency; the dev push full run is the backstop for it, as for every hidden input.

Manifest from run [38958729422](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38958729422): 32389 tests, 9385 units, 5062 tests that start a process.

| Number | Static imports | Manifests |
|---|---|---|
| Reusable share over the last 40 dev merges, mean | 34.9 % (29.9 % with data files) | 68.7 % |
| Same, median | 0 | 82.9 % |

Measured with `python -m tests.dep_manifest share --manifest manifest.json --ref origin/dev --merges 40` on 2026-10-16. The share applies that one manifest to each merge diff, so it measures the invalidation rule, not 40 separate records; a unit renamed or removed since a merge cannot be hit, which leans the figure upward. 7 of the 40 merges reuse nothing: five `.github/` changes, one Python guard under `profiles/` and one shared helper under `tests/`. Most other merges invalidate about 5100 tests, the process starting tests included, because nearly every merge changes `hooks/` or `scripts/`.

| Run | Plant | Result |
|---|---|---|
| [38958729422](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38958729422) | none | every job green; the manifest above |
| [38954524795](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38954524795) | `scripts/doctor/rates.py::table` raises on entry | 4 tests failed on 3.12 and the same 4 on 3.11; those are exactly the 4 tests whose manifest units hold that function. `invalidated` from the base to the plant commit returns those 4 plus the 5062 process starting tests, which every source change invalidates by rule, and nothing else |

Cost, from run [38952385459](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38952385459) against queue runs 38950629400 to 38956012323: 3.12 Run tests mean 217 s against 178 to 210 s, slowest shard 227 s against 198 to 246 s; `sonar` 185 s against 168 s on dev push run 38950625810, of which the merge wait grew to 37 s and the build takes 24 s; the 12 coverage artifacts grew from 19 MB to 202 MB; `coverage-ratchet` 84 s against 80 s.

Not done here: the queue run's read of the dev cache entry, which slice 5 needs, is still unproven.

## Slice 5 as delivered

Merge queue runs, and gate plant dispatches, skip every test whose manifest inputs did not change since a dev push in which it passed.

- **Record.** `scripts/ci_test_plan.py` reads the last 10 completed dev push Tests runs and takes the newest one whose 18 unit shards passed and that kept a `dep-manifest` artifact whose commit is the run's head. Both interpreter legs ignore dev runs that changed after their own run started, so they pick the same record. It does not require the whole run to pass, because promotion checks such as `helm-kind` fail dev pushes without touching the unit results. No such run is a missing record: the planner raises a `No test record` notice and every test runs.
- **Plan.** Each `split` leg adds the protected grader as a worktree. For a queue run the grader is the merge group base; for a dispatch it is the head. The leg runs the planner from that worktree with the interpreter outside the test venv. Every manifest test that the diff from the record commit to the head leaves valid is reused, keyed without its xdist group suffix. The leg publishes the plan as `reused-tests-<python>`. The protected gate freezes the durations job's outputs, so the plan cannot live there.
- **Deselect.** `tests.dep_manifest deselect`, run from the same grader worktree with the same interpreter, intersects the plan with the head collection and also lists the test files whose collected tests are all reused. It publishes `reused-<python>`. Each unit shard downloads that list into `CI_REUSED`. `conftest` leaves the wholly reused files uncollected and drops reused nodes from the duration split, so shards balance on the tests that remain, and then deselects the remaining reused items. A shard that has nothing left passes.
- **Grade.** `shard-check` runs from the base and passes the grader's plan to `shard_check --reused`. Collected tests the plan does not reuse must each run exactly once, and reused tests must not run at all. Its step summary prints the counts of tests ran, tests reused (naming the dev run that recorded them) and flaky tests for each Python version.
- **Dev push.** `ci_dev_reuse` refuses a queue run that published a plan, so a dev push runs the full suite and records the next manifest.

Coverage: merge group runs feed no coverage consumer, since `sonar` and the ratchet do not run there. The only reader of queue coverage is dev unit reuse, and it is refused for partial runs, so no combine is needed on this path. Pull request full scope runs stay on the full suite. `select` sends a pull request to full scope only for changes that the manifest rule also treats as global, such as `.github/` and `pyproject.toml`.

A merge whose base grader has no planner, which includes the queue run of this slice's own pull request, runs in full.

What the queue can reuse, measured with the planner's own rule: the manifest of dev push run [38972462506](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38972462506) (32678 tests, commit `5320236c9`) applied to each of the 40 dev merges before it, with `python -m tests.dep_manifest share --merges 40`. A queue entry reuses on average 58.0 % of its tests, median 79.2 %. 10 of the 40 merges reuse nothing: six changed `.github/`, and at least three changed a shared helper under `tests/`, which invalidates every test. Each 3.12 shard runs about 217 s of tests in its Run tests step, so at the median the remaining tests split over the same 12 shards run about a fifth of that.

Pull request runs and the coverage combine are not in this slice. `select` already narrows a pull request to the tests its change reaches. It falls back to the full suite only for a harness or `conftest.py` change, which the manifest rule also treats as global, for a change with no selected tests, or when the selected tests exceed three legs. A full scope pull request run feeds `sonar` and the coverage ratchet, so reusing tests there needs coverage combined from the dev record. That is proposed as its own follow up.

Known limits. Each is detected only after the merge, by the next dev push, which runs every test; a red dev push does not hold the queue:

- The manifest is measured on 3.12, the only Python the suite runs.
- A test that reads a Python source as text, or lists a directory, depends only on what it executed or opened.
- A test that passed the record run only on a retry is still reused.
- A dispatch plans from its own head, since gate plant dispatches need admission by a trusted actor.

## Slice 6 as delivered

Each keyed job declares its inputs in `.github/input-keys.json`: repository paths at the head, base paths read at the grading base, and the tools it runs. `scripts/ci_input_key.py` hashes the git object of every declared path, the keyer's own script and action, the declared tools, the runner (`RUNNER_OS`, `RUNNER_ARCH`, `RUNNER_ENVIRONMENT`, `ImageOS`, `ImageVersion`), any resolved external input the job passes, such as the Redis service image id, and the UTC day. A declared path missing at its revision fails the step.

`.github/actions/input-key` looks the key up with `actions/cache/restore` in lookup only mode. On a hit every heavy step is skipped, the job stays green and raises an `Input key reused` notice naming the key. On a miss the heavy steps run, and `.github/actions/input-key/save` saves the key after they pass, only on push and dispatch runs. A cache entry is visible only to runs of the ref that saved it, so dev runs read only keys written by runs of code already on dev.

`ledger-load` keys its Redis service image id and `brain-smoke` its collector image id, both read with `docker inspect`. Both adopt it. Over the last 100 dev merges about half changed none of their declared inputs.

| Run | Plant | Result |
|---|---|---|
| [38961789273](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38961789273), [38961792398](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38961792398) | none, first run of the head | brain smoke 33 s and ledger load 139 s: key missed, every step ran, key saved |
| [38963275517](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38963275517), [38963279568](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38963279568) | a docs change, outside every declared input | 20 s each: key hit, install, check and save skipped |
| [38963740312](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38963740312), [38965443542](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38965443542) | a change to `hooks/` and `tests/ledger_load/`, both declared | brain smoke 37 s and ledger load 131 s: key missed, every step ran, the new key saved |

Known limits: the day in the key forces one full run per job per UTC day, which also bounds how long an unkeyed floating input, such as a Python patch release or a self hosted runner image, can be reused. `ci_reuse.py` keeps the whole tree queue reuse. `kind-due` looks up the `helm-kind` key of the chart, the swarm image inputs and the kind proof files on the same runner pool, and a hit skips `helm-kind`.

## Helm kind proof input keys

The Helm chart proof uses the same job input key in both `kind-due` in Tests and the reusable Helm chart in kind workflow. The former hand written path rule is removed. On a dev push, `kind-due` checks out the graded head and looks up `helm-kind`; `due` is true only when the key misses. Both jobs use the same runner pool so the runner fields in their keys match.

| Decision | Tests workflow | Direct Helm workflow or Proofs dispatch |
|---|---|---|
| Passed key found | `kind-due` succeeds with `due=false`; `helm-kind` is skipped | The job succeeds; kind installation, fixture layer restore and chart proof steps are skipped |
| Key missing | `due=true` calls the Helm workflow, which checks the key again | Install kind and run the full chart proof; save the passed key after success on a push or dispatch |
| Input lookup fails | The due job fails; the chart workflow is not called | The job fails before the proof; no passed key is saved |

The required gate accepts a skipped `helm-kind` when `due=false`. It still checks the due job's result. In the current delivery tiers, Tests invokes `kind-due` on pushes and admitted gate plant dispatches; pull requests and merge groups do not run this promotion proof. Use a focused Proofs dispatch to check a branch manually.

### Declared inputs and configuration

The `helm-kind` entry in `.github/input-keys.json` is the declaration to maintain when the chart proof gains an input. It covers:

- The whole `deploy/helm/agentihooks-swarm` chart, including its CI fixtures, and `.github/workflows/helm-kind.yml`.
- Swarm image inputs: `Dockerfile`, `.dockerignore`, `pyproject.toml`, `README.md`, `docker/swarm/requirements.lock`, `docker/swarm-node`, `hooks`, `scripts`, `profiles` and `media`.
- Proof schemas and fixtures: `docs/swarm-v2/schemas`, `tests/fixtures/swarm_v2`, `tests/kind_request.py` and the five `tests/chart_*.py` helpers named in the declaration (`chart_hive_join`, `chart_network`, `chart_redis`, `chart_secrets`, `chart_workers`).
- Tool labels `kind v0.33.0` and `kindest/node v1.37.0`. Keep these aligned with `KIND_VERSION` and `KIND_NODE_IMAGE` in the Helm workflow; the workflow itself is also a declared input, including the pinned node image digest.

For example, if a new chart probe reads `tests/chart_storage.py`, append that path to the existing `helm-kind.paths` array. Preserve the other entries. When upgrading kind, update both the workflow environment and the declaration's tool label:

```yaml
# .github/workflows/helm-kind.yml, jobs.kind.env
KIND_VERSION: v0.33.0
KIND_NODE_IMAGE: mirror.gcr.io/kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
```

```json
"tools": ["kind v0.33.0", "kindest/node v1.37.0"]
```

The key also includes the shared keyer's implementation, runner identity and UTC day, as described under [Slice 6](#slice-6-as-delivered). Helm passes only `job: helm-kind` to the input-key action; it supplies no grading base or external environment argument. A docs edit outside the declared paths can reuse a passed key, but editing `README.md`, a schema, a script or a profile invalidates it even when no chart template changed. A new day or changed runner image can also cause a full run. Cache eviction or a missing passed record safely falls back to the full proof. A failed proof saves no passed key.

### Run and inspect the proof

Dispatch the focused proof for a pushed branch:

```bash
gh workflow run proofs.yml \
  --ref <branch> \
  -f proof=helm-kind
```

The reusable workflow can also be dispatched directly with `gh workflow run helm-kind.yml --ref <branch>`. Neither dispatch offers a force or cache bypass input: dispatching the same inputs on the same day can reuse the previous pass. A passed branch dispatch does not seed the dev branch's pass record.

Inspect the job and step conclusions for a chosen Tests or Proofs run:

```bash
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run-id>/jobs \
  --paginate \
  --jq '.jobs[] | select(.name | contains("kind")) | {name, conclusion, started_at, completed_at, steps: [.steps[] | {name, conclusion}]}'
```

On a hit, the lookup emits an `Input key reused` notice naming `helm-kind` and its key. In Tests, inspect `kind-due` and the skipped `helm-kind` job. In a direct or Proofs run, inspect the successful job's skipped heavy steps. Measure job wall time from `started_at` to `completed_at`; a step skip and a skipped job are different results.

The fixture image layer cache is separate. It accelerates image builds on a proof key miss, can restore older layers with the `kind-layers-` prefix, and saves only when an export is complete. A layer hit still runs the chart proof. Only a passed proof input key authorizes skipping it.

### Delivery evidence and limits

[Pull request 3866](https://github.com/the-cloud-clockwork/agentihooks/pull/3866) records the baseline: Helm took 151 to 191 seconds on twelve of fifteen dev pushes, with `kind-due` taking 12 to 16 seconds. The [source issue's completion report](https://github.com/the-cloud-clockwork/agentihooks/issues/3865) records branch misses of 169 and 178 seconds and a hit of 10 seconds. In [Proofs run 39000899419](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39000899419), the successful Helm job skipped kind installation, layer restoration, the chart proof and pass saving.

The first reported dev push after merge, [39011568629](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39011568629), missed the cache and ran the full proof in 188 seconds before saving the key; the due lookup took 11 seconds. The source completion report explicitly leaves the unchanged-input dev-push skip measurement outstanding. These measurements prove branch reuse and an initial dev miss; they do not establish a measured dev-push speedup or a separate chart-input-change full-run comparison. Record those run links and job timings when that source proof is available.

## Full scope pull request reuse as delivered

Pull requests into dev from a branch of this repository plan reuse the same way the queue does. A full scope run, one that `select` did not narrow to the tests its change reaches, skips every test that passed on the dev record with unchanged inputs and carries those tests' dev coverage into its own, so `sonar` and the coverage ratchet still grade the whole suite. A narrowed run skips the `split` job, so it plans nothing and runs only its selected tests.

### When a pull request run reuses tests

The `split` job's plan step runs on `merge_group`, on `workflow_dispatch`, and on a `pull_request` event when all of these hold:

- the base branch is `dev` and the head branch lives in this repository; a fork pull request plans nothing;
- the grader, a detached worktree of `origin/dev`, has `scripts/ci_test_plan.py`;
- the grader also has `tests/reused_coverage.py`. A grader without it cannot carry reused coverage, so the step prints `The grader cannot carry reused tests' coverage into Sonar; every test runs.` and plans no reuse. Merge group runs skip this check. A dispatch's grader is its own head, so it runs the check and the coverage filter from the head.

The planner runs from the grader, never from the head, so a pull request cannot widen what it reuses. It mints the CI App token with `actions: read` to read dev's runs and artifacts.

### Record

`scripts/ci_test_plan.py` keeps the slice 5 rule and adds one condition: the dev push run must also keep all twenty `coverage-3.12-1` to `coverage-3.12-20` artifacts unexpired, beside its `dep-manifest`. A run missing any of them is skipped and the next older candidate is tried, up to 10. With no record the planner raises the `No test record` notice and every test runs.

Besides `reused`, the planner writes `run` and `commit` to the step output: the dev run that holds the record and the commit its manifest was measured on. `split` passes both to `unit`.

### Reused coverage on each shard

On every 3.12 unit shard of a full scope run that reuses at least one test, outside merge groups:

1. **Token.** `Mint the token that reads the dev coverage` mints the CI App token with `actions: read`.
2. **Download.** `Download this shard's dev coverage` downloads the same shard's `coverage-3.12-<shard>` artifact from the record run into the runner's temporary folder.
3. **Filter and remap.** `Keep the reused tests' dev coverage` adds the grader as a worktree (`origin/dev` for a pull request, the head for a dispatch) and runs `python -m tests.reused_coverage` from it, so the filter that moves dev coverage is protected code.

`tests.reused_coverage` writes `reused-coverage/.coverage` and adds page captures to the shard's `js-coverage/`:

- **Contexts.** It keeps the arcs of the per test contexts whose test is in the shard's `reused.json`, plus arcs with the empty context, which coverage records at import time. Contexts of tests this run executes are dropped, so their coverage comes from this run only.
- **Changed sources.** For every Python file that differs between the record commit and the head, it maps old line numbers to new ones with a line diff and moves each kept arc onto the head line. An arc whose end lines no longer exist is dropped. A file deleted at the head is removed from the data. A reused test ran only code whose text did not change, so its arcs land on the same statements at their new lines.
- **Page captures.** Browser and node captures now record the test that took them in a `test` field (`tests/browser_coverage.py` reads `PYTEST_CURRENT_TEST` when the page opens, `tests/node_capture.cjs` when the node process starts). A capture whose test is not reused is dropped. A capture with no test, taken outside a test or written before captures carried the field, is kept whole.

The step prints `Kept the dev coverage of <n> reused tests and <m> page captures from <commit>; <k> changed files remapped`. Each shard uploads `reused-coverage/` inside its `coverage-3.12-<shard>` artifact.

### Old file fetch in the remap

The remap runs against the unit job's own checkout (`--repo "$GITHUB_WORKSPACE"`), a partial clone with `filter: blob:none` (see [Unit checkout](../ci-timing.md#unit-checkout)). That checkout holds the file contents of the graded commit only. The old text of every changed file, at the record commit, is missing until git fetches it from the remote.

`tests.reused_coverage.moved_lines` lists the changed files with:

```bash
git -C "$GITHUB_WORKSPACE" diff --no-renames --numstat "<record commit>" HEAD -- '*.py'
```

`--numstat` counts added and deleted lines, so git needs both texts of every listed file and fetches all the missing old blobs in one `git fetch` before it prints. The remap then reads each file's old and new text with `git show <commit>:<path>`, which finds every blob locally. The path is the third tab separated field of each `--numstat` line. The file list and the line maps are the same as before the change.

Before [pull request 4828](https://github.com/the-cloud-clockwork/agentihooks/pull/4828) ([#4825](https://github.com/the-cloud-clockwork/agentihooks/issues/4825)) the list came from `git diff --name-only`, which reads no file contents. Each `git show` of an old file then started its own lazy fetch, one network round trip per changed file. The step's time grew with the number of files changed since the record commit:

| Run | Changed files remapped | `Keep the reused tests' dev coverage` | Tier wall time |
|---|---|---|---|
| Pull request run [39251854559](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39251854559), attempt 1, before | 408 on unit (3.12, 1) | 230 to 256 s on every 3.12 shard, 256 s on unit (3.12, 1) | 1205 s, over the 900 s Doctor threshold |
| Pull request run [39304815170](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39304815170), attempt 1, after | 9 | at most 9 s | 717 s |

The fix lives in the grader's copy of `tests/reused_coverage.py`: `origin/dev` for a pull request, the head for a dispatch. Every pull request run created after the merge, 2026-10-19 18:06 UTC, uses it, whatever its branch holds. A dispatch from a head older than the merge still fetches one file at a time.

#### Proof

- Red: CI run [39276467640](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39276467640) on the tests alone failed `test_a_partial_clone_fetches_every_changed_file_old_text_in_one_batch` with `assert 3 == 1`: three changed files, three fetches.
- Green: CI run [39276717099](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39276717099) on the fix passed.
- Controlled timing: CI run [39310021504](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39310021504) timed both listings of the same 376 changed files, each on a fresh `blob:none` clone from GitHub, and printed `REMAP_TIMING files=376 same=True name_only_seconds=245.7 numstat_seconds=3.5`. Both listings produced identical line maps.

The test builds an origin repository with three changed files, each with its own old text, clones it with `--filter=blob:none`, sets `GIT_TRACE2_EVENT` and counts the `git fetch` child processes that `moved_lines` starts. Identical old texts share one blob and would need one fetch even without the fix, so every file gets distinct text.

#### Reading the remap time on a run

The Doctor measure prints the run attempt's tier wall time in seconds; the threshold is 15 minutes, 900 s:

```bash
agentihooks doctor rig-grade-swarm measure "ci-tier-wall-time/<run id>-<attempt>"
```

The wall time of a finished run never changes, so measuring the same run before and after the fix prints the same figure. Compare the remap step on runs created before and after the change instead:

```bash
RUN="<tests run id>"
gh api --paginate "repos/the-cloud-clockwork/agentihooks/actions/runs/$RUN/attempts/1/jobs?per_page=30" \
  --jq '.jobs[] | select(.name | startswith("unit (3.12")) | .name as $job | .steps[]
        | select(.name | startswith("Keep the reused tests"))
        | "\($job) \((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601)) s"'
gh run view "$RUN" --log | grep "changed files remapped"
```

The first command prints each 3.12 shard's remap step time; the second prints how many files each shard remapped. For the same 376 changed files, the controlled run under Proof took 3.5 s with `--numstat` and 245.7 s with `--name-only`.

| Symptom | Read | Action |
|---|---|---|
| The remap step takes minutes and its time grows with `<k> changed files remapped` | The run's event and the grader: `origin/dev` for a pull request, the head for a dispatch | A dispatch from a head older than pull request 4828 lists files with `--name-only`; merge `origin/dev` into the branch and dispatch again |
| The remap step takes minutes with few changed files | The step log for a stalled fetch | The batch fetch itself is slow or stalled; see [Unit checkout](../ci-timing.md#unit-checkout) for the checkout's fetch behavior |

### Unusable coverage databases

`tests/coverage_check.py` decides whether a coverage data file can be read. Two steps use it: the reuse step above, before it reads the downloaded dev database, and `Check the coverage database`, before each 3.12 unit shard uploads its own.

`coverage_check.problem` opens the file read only and returns the first problem it finds, or nothing for a usable database:

| Problem text | Cause |
|---|---|
| `missing` | No file at the path. |
| The SQLite error, such as `file is not a database` or `database disk image is malformed` | SQLite cannot open or read the file. A truncated or overwritten download lands here. |
| The `pragma integrity_check` result | SQLite opens the file but the integrity check does not answer `ok`. |
| `missing tables <names>` | One of `arc`, `context`, `file` or `line_bits` is absent. Reuse and `.github/coverage/combine.sh` read all four. |

**Reuse step.** When the downloaded dev database has a problem, `python -m tests.reused_coverage` prints one error annotation and exits 1 before reading `reused.json` or the database:

```text
::error::The dev coverage coverage-3.12-9 from <commit> is unusable (database disk image is malformed)
```

The artifact name comes from the step's `ARTIFACT` variable, `coverage-<python>-<shard>`; without it the message names the file path. The commit is the record commit from `split`. The step fails, so the shard fails before its tests run. `reused.json` is left as `split` planned it, so the other shards are graded against the same plan.

**Upload step.** `Check the coverage database` runs on every 3.12 unit shard, on every event, after the tests and before `Upload coverage`. It checks `.coverage` and, when the shard wrote one, `reused-coverage/.coverage`, and prints one line per bad file:

```text
::error::The coverage database reused-coverage/.coverage is unusable: missing tables line_bits
```

Any bad file fails the step, so nothing is uploaded and the unit job fails. A dev push whose unit shard fails this way is never chosen as a record, because the planner takes only a run whose unit shards all passed. A database that is already bad on the runner therefore stops at the dev push that wrote it instead of reaching a later pull request. The check cannot see damage that happens after upload, so the reuse step keeps its own check.

Run the check by hand on a downloaded artifact:

```bash
gh run download "<dev run id>" --name coverage-3.12-9 --dir "<scratch dir>"
python -m tests.coverage_check "<scratch dir>/.coverage" "<scratch dir>/reused-coverage/.coverage"
echo "exit $?"
```

It prints nothing and exits 0 when every file is usable. A file that is not given on the command line is not checked; the workflow adds `reused-coverage/.coverage` only when it exists.

**History.** [Pull request 4845](https://github.com/the-cloud-clockwork/agentihooks/pull/4845) ([#4842](https://github.com/the-cloud-clockwork/agentihooks/issues/4842)) added the check after pull request 4755 failed unit (3.12, 9) in run [39235643311](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39235643311). The reuse step there raised `sqlite3.DatabaseError: database disk image is malformed` on artifact `coverage-3.12-9` from dev push run [39232564179](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39232564179), whose own unit (3.12, 9) job had passed. As delivered, a bad dev database made the reuse step print a warning, delete `reused.json` and exit 0, so the shard ran its reused tests. Red: Proofs run [39291713004](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39291713004) on the tests alone failed both new tests, one with `sqlite3.DatabaseError: database disk image is malformed` and one with `KeyError: 'Check the coverage database'`. Green: Proofs run [39291717982](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39291717982) passed.

[Pull request 5245](https://github.com/the-cloud-clockwork/agentihooks/pull/5245) ([#5145](https://github.com/the-cloud-clockwork/agentihooks/issues/5145)) replaced that fallback with the failure above. A shard that dropped its reuse plan split the full collection while the other shards split the collection without reused tests, so `shard-check` turned red on a correct change: plant run [39378597454](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39378597454) reported 4730 of 38995 tests that ran zero times or more than once. The upload check from pull request 4845 is unchanged.

**Troubleshooting.**

| Symptom | Read | Action |
|---|---|---|
| `Keep the reused tests' dev coverage` fails with `The dev coverage ... is unusable` | The named artifact in the record run from `split` | Rerun the whole Tests run after the next dev push records a new run, or download the artifact and run the check by hand to confirm the problem |
| `Check the coverage database` fails on a dev push | The problem text for `.coverage` or `reused-coverage/.coverage` | Rerun the job; the run is not used as a record while that shard is red |
| `Check the coverage database` reports `missing` for `.coverage` | The `Run tests` step of the same shard | The shard wrote no coverage data; fix the earlier failure first |
| `shard-check` reports tests that ran zero times or twice after a coverage warning, not an error | The grader of the run: `origin/dev` for a pull request, the head for a dispatch | A dispatch from a head older than pull request 5245 still deletes `reused.json`; merge `origin/dev` into the branch and dispatch again |

### Combine and grade

- `.github/coverage/combine.sh`, which builds the report `sonar` reads, adds each shard's `reused-coverage/.coverage` when the file exists and is not empty.
- `tests/coverage_ratchet.py` adds every `coverage-3.12-*/reused-coverage/.coverage` it finds to the head measurement.
- Merge group runs never produce `reused-coverage/`, so both read the shards' own data as before.

### Checking a run

```bash
RUN=<tests run id>
# Planner decision on each split leg
gh run view "$RUN" --log | grep -E "Dev run [0-9]+ on|No dev push run with passed|cannot carry reused"
# Reused coverage kept on each 3.12 unit shard
gh run view "$RUN" --log | grep "Kept the dev coverage of"
# Unusable dev or shard coverage databases
gh run view "$RUN" --log | grep -E "The (dev coverage|coverage database) .* is unusable"
# Ran, reused and flaky counts from both shard-check legs
gh run view "$RUN" --log | grep -E "Python 3\.1[12]: ran [0-9]+, reused"
```

`shard-check`'s step summary prints `Python 3.12: ran <n>, reused <m> from <dev run>, flaky <k>`.

### Proof and evidence gap

- Prototype on dev run [39014881228](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39014881228), shard 1: with every test reused the filtered data kept arcs identical to the dev shard; filter and remap took 0.1 to 0.9 s. Each shard's coverage file is about 50 MB with per test contexts.
- The source pull request changed `.github/`, which invalidates every test, so its own runs reused nothing. Dispatch 39022758285 planned from dev run 39021960767 and found all 33788 tests invalidated by `.github/CODEOWNERS`.
- Open: a CI comparison of a reusing full scope run against a full run of the same head, coverage within the ratchet. It needs a dev record written after this change, so page captures carry their test, and a head whose change is not global. The first such record is dev push run [39026074158](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39026074158).

### Known limits

- Everything listed under slice 5's known limits applies: the manifest is measured on 3.12, and the next dev push, which runs every test, is the backstop for any hidden input.
- Reused coverage is the dev record's measurement moved by a line diff. A changed line that the diff matches to an unchanged one elsewhere can carry an arc onto it; the next dev push measures the real figure.
- Only `*.py` files are remapped, and the diff runs with `--no-renames`: a renamed file counts as deleted, so the reused tests' arcs on it are dropped and that coverage is missing until the next dev push.
- A page capture with no test is kept whenever any test is reused, so it can carry coverage from tests that ran on the dev record but run again here.
- The download of the dev shard's coverage has no fallback: if the artifact cannot be downloaded, the shard fails instead of running every test. An unusable downloaded database also fails the shard; see [Unusable coverage databases](#unusable-coverage-databases).
- A dev record whose coverage artifacts expired, after 7 days, is never chosen, so a quiet dev plans no reuse until its next push.

## A crashed xdist worker and coverage data

A unit shard runs its tests on pytest-xdist workers with pytest-cov measuring each worker. When one worker crashes, xdist starts a replacement. The replacement leaves the other workers' coverage data files in place, so the crash costs the test that was running on the crashed worker and nothing else. Before [pull request 5034](https://github.com/the-cloud-clockwork/agentihooks/pull/5034) ([#5023](https://github.com/the-cloud-clockwork/agentihooks/issues/5023)), the replacement deleted those files and every later test on the other workers errored.

### Cause

xdist starts every worker with the controller's command line, `-n` included. pytest-cov reads `-n` as a distributed run and starts a `DistMaster` controller inside the worker as well. `DistMaster.start` calls `cov.erase()` unless `--cov-append` is set, and with `parallel` data `erase` removes every `.coverage.*` file next to the data file. At session start every worker does this before any test has written data, so nothing is lost. A replacement worker starts mid run and removes the data files of the workers that are still running. Each of those workers then fails its next coverage write with `no such table: context`, and every later test on it errors.

The case that started the investigation was dev push Tests run [39316644629](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39316644629), unit (3.12, 4), job [118282774339](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39316644629/job/118282774339):

```text
2026-10-19T20:16:20.2074431Z Timeout (0:01:00)!
2026-10-19T20:16:21.0419070Z [gw2] node down: Not properly terminated
2026-10-19T20:16:21.0424783Z replacing crashed worker gw2
...
INTERNALERROR> E   coverage.exceptions.DataError: Couldn't use data file '/home/runner/_work/agentihooks/agentihooks/.coverage.anton-oss-xstf7-runner-xvwwh.pid412.XboGAMjx': no such table: context
==== 2392 passed, 2 warnings, 1208 errors, 2419 rerun in 508.32s (0:08:28) =====
```

One worker timeout cost 1208 errors and an `INTERNALERROR` on the shard.

Proofs run [39352981080](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39352981080) showed which code removes the files. A probe crashed one worker on purpose with a Python audit hook recording every `os.remove`. In each variant the replacement worker's stack went through `self.start(engine.DistMaster)`, then `self.cov.erase()`, then coverage's `sqldata.erase`, then `os.remove`, removing another worker's `.coverage.<host>.pid<n>.<suffix>` file.

### The fix

`tests/browser_coverage.py` implements `pytest_load_initial_conftests` as a wrapper hook. When `PYTEST_XDIST_WORKER` is set, which xdist does only in a worker, it sets `early_config.known_args_namespace.cov_append = True` before pytest-cov reads its options. A worker's pytest-cov then skips `cov.erase()`. The controller has no `PYTEST_XDIST_WORKER`, so it still erases old data once at the start of the run, and a fresh run never mixes in data left by an earlier one.

The plugin is loaded with `-p tests.browser_coverage`. The 3.12 legs of the `unit` and `unit-selected` jobs in `.github/workflows/test.yml` pass it in `PYTEST_ADDOPTS` beside the `--cov` options:

```yaml
PYTEST_ADDOPTS: ${{ matrix.python-version == '3.12' && format('--cov=hooks --cov=scripts --cov-report= --cov-config={0}/.github/coverage/coverage.ini --cov-context=test -p tests.browser_coverage -p tests.dep_inputs', github.workspace) || '-p no:pytest_cov' }}
```

The other Python versions run with `-p no:pytest_cov` and write no coverage data, so they never had the problem. The worker branch needs no setting and works without `JS_COVERAGE_DIR`; the rest of the plugin, the JavaScript page capture, stays idle unless `JS_COVERAGE_DIR` is set. Any pytest run that measures coverage under xdist gets the same protection when it adds `-p tests.browser_coverage`:

```bash
python -m pytest tests/some_test.py -n 4 --dist loadgroup \
  --cov=hooks --cov=scripts --cov-report= --cov-context=test \
  -p tests.browser_coverage
```

A crash still fails the crashing test, and with `--reruns` that test is rerun on the replacement worker. The other workers keep their data, so the shard's coverage report still includes the tests they ran.

### The crash probe test

`tests/test_xdist_coverage_crash.py::test_a_replacement_worker_keeps_the_live_workers_coverage_data` writes a small project to a temporary folder and runs a nested pytest in it with three workers, `--dist loadgroup`, `-p tests.browser_coverage`, `--cov=.` and `--cov-context=test`. Its `test_crash` calls `os._exit(1)` the first time it runs, and 60 `test_ok` cases each sleep 0.3 seconds so the other workers are still running when the replacement starts. The test requires:

- `replacing crashed worker` in the output, so a worker really was replaced;
- no `ERROR` result line from any worker;
- all 60 `test_ok` cases `PASSED`;
- exactly one `FAILED` line, for `test_cases.py::test_crash`.

The nested run strips `COV_CORE_*`, `COVERAGE_*` and `PYTEST_*` from its environment so the outer shard's coverage and xdist settings do not leak into it, and it is killed after 120 seconds.

The same file also holds `test_a_run_whose_last_test_crashes_its_worker_ends`, from [pull request 5061](https://github.com/the-cloud-clockwork/agentihooks/pull/5061). It covers a different failure: a run whose last test crashes its worker never ends under xdist's plain scheduler. It also checks that every `test_ok` case still has its coverage context in the combined `.coverage`, which only holds while workers keep each other's data.

Run the probe test on a branch through the Proofs workflow; tests do not run on this machine:

```bash
gh workflow run proofs.yml --ref "<branch>" -f proof=tests -f tests='tests/test_xdist_coverage_crash.py'
```

### Measured result

The number moved is the errors one deliberately crashed worker causes in the probe.

| Run | Code | Result |
|---|---|---|
| [39316644629](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39316644629) | dev push, before the fix | one worker timeout, 1208 errors on unit (3.12, 4) |
| [39356878790](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39356878790) | probe test without the fix | nested run `1 failed, 2 passed, 117 errors`, probe test red |
| [39356881427](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39356881427) | probe test with the fix | probe test passed: no errors, 60 of 60 `test_ok` passed, only `test_crash` failed |

Only 2 of the 60 `test_ok` cases passed in the nested run without the fix.

### Troubleshooting

| Symptom | Read | Action |
|---|---|---|
| After `replacing crashed worker gwN`, many tests on other workers error, and the log shows `no such table: context` or `Couldn't use data file '.coverage.…'` | The job's `PYTEST_ADDOPTS` | The run measures coverage under xdist without `-p tests.browser_coverage`. Add it next to the `--cov` options. |
| The same errors with `-p tests.browser_coverage` loaded | `pytest_load_initial_conftests` in `tests/browser_coverage.py`, and whether the run sets `PYTEST_XDIST_WORKER` itself | A run that exports `PYTEST_XDIST_WORKER` in the controller makes it skip its own start up erase. Remove it from the environment. |
| A shard ends with `node down: Not properly terminated` and one failed or rerun test, other workers green | The crashed test's output before the `node down` line | Expected: the crash cost one test. Fix the crashing test. |
| A shard never finishes after a crash on its last test | [Pull request 5061](https://github.com/the-cloud-clockwork/agentihooks/pull/5061) | Load `-p tests.flake_ledger`, whose scheduler drains the empty units a crashed worker leaves. |

## Retried duration uploads

`.github/actions/retry-upload` is the upload side of the same rule ([#3996](https://github.com/the-cloud-clockwork/agentihooks/issues/3996), [#3998](https://github.com/the-cloud-clockwork/agentihooks/pull/3998)). It uploads one artifact of the run with `actions/upload-artifact@v4`, up to three attempts. Attempts 1 and 2 are `continue-on-error`; each failure raises a `Retried upload` warning annotation and waits 10 s, then 20 s. Attempt 3 is not, so an upload that never succeeds still fails the step and the job. There is no workflow rerun.

| Input | Required | Default | Passed to every attempt as |
|---|---|---|---|
| `name` | yes | | `name` |
| `path` | yes | | `path` |
| `retention-days` | yes | | `retention-days` |
| `include-hidden-files` | no | `false` | `include-hidden-files` |
| `if-no-files-found` | no | `warn` | `if-no-files-found` |

Every attempt sets `overwrite: true`, so a caller does not pass it. A retry deletes any copy an earlier attempt finalized under the same name and uploads the files again: the artifact keeps the caller's name, files and retention, whichever attempt succeeds. `if-no-files-found` reaches every attempt, so a caller that asks for `error` stays red when its files are missing: all three attempts report no files and the step fails, 30 s later than a plain upload would.

Four steps in `test.yml` use it; every other upload in the workflow calls `actions/upload-artifact@v4` directly.

```yaml
      - name: Upload the chosen durations        # split
        uses: ./.github/actions/retry-upload
        with:
          name: split-${{ matrix.python-version }}
          path: |
            .test_durations
            durations.sha256
          include-hidden-files: true
          if-no-files-found: error
          retention-days: 7

      - name: Upload durations                   # unit
        if: ${{ !cancelled() && hashFiles('durations.json') != '' }}
        uses: ./.github/actions/retry-upload
        with:
          name: durations-${{ matrix.python-version }}-${{ matrix.shard }}
          path: |
            durations.json
            durations.sha256
            elapsed.json
          retention-days: 14
```

To route another upload through it, replace `uses: actions/upload-artifact@v4` with `uses: ./.github/actions/retry-upload` and drop `overwrite`. Do it only for an upload whose name is unique to the job, since each attempt replaces the artifact of that name. `tests/test_ci_step_retry.py` pins the three attempts, the waits and warnings, the inputs passed to each attempt and the `with` block of both duration steps and of the unit flake report step. Its guard against retrying in a job that holds a deployment environment or an OIDC token covers only `retry-download`; nothing stops `retry-upload` there, so keep it out of such jobs by review.

Why the action's own retry is not enough, read from its source for [#3996](https://github.com/the-cloud-clockwork/agentihooks/issues/3996): `actions/upload-artifact@v4` resolves to v4.6.2, `actions/upload-artifact` commit [`ea165f8`](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02). Its artifact service client retries timeouts and retryable HTTP statuses up to five times, but throws a socket error code (`ECONNRESET`, `ECONNREFUSED`, `ETIMEDOUT`, `ENOTFOUND`, `EHOSTUNREACH`) at once. The toolkit's `main` branch does the same, so a version bump does not help.

Before: on 2026-10-16, 3 of 2369 executed duration upload steps failed, in 2 of 130 queue attempts. The two failure classes stay separate; no common cause is shown.

| Step | Executed | Failed | Error |
|---|---:|---:|---|
| Upload the chosen durations (`split`) | 254 | 2 | `Failed to CreateArtifact: Unable to make request: ECONNREFUSED`, after four `ListArtifacts` request timeouts |
| Upload durations (`unit`) | 2115 | 1 | `Failed to FinalizeArtifact: Unable to make request: ECONNRESET`, after the content uploaded |

The flake report, coverage and collected tests uploads in the same jobs had no failures. In the `FinalizeArtifact` case the same job's next upload finalized two seconds later and three other runners uploaded in the same seconds, so no pool wide outage was visible in the seconds around it. The `CreateArtifact` case has no concurrent control upload, and one of its two job logs has expired.

| Run | Fault injected on a plant branch | Result |
|---|---|---|
| [39025447958](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39025447958) | `ECONNRESET` on `FinalizeArtifact`, plain `upload-artifact@v4` | `Failed to FinalizeArtifact: Unable to make request: ECONNRESET`, job red: the 16 October message |
| same run | `ECONNRESET` on `FinalizeArtifact`, `retry-upload` | attempt 1 red, attempt 2 finalized; the downloaded sha256 matches the upload |
| same run | `ECONNREFUSED` on `CreateArtifact`, `retry-upload` | attempt 1 red, attempt 2 finalized; the downloaded sha256 matches the upload |
| same run | missing file with `if-no-files-found: error`, `retry-upload` | three `No files were found` attempts, job red |
| [39025246357](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39025246357) | tests first head | only the two new routing tests failed |
| [39025583932](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39025583932) | delivered head | green; all 20 duration uploads succeeded through the composite |

The fault was injected with a `NODE_OPTIONS` preload that breaks the first matching artifact service request with the given socket error.

Troubleshooting: a `Retried upload` warning followed by a green step means one or more failed attempts were replaced by a later success; nothing needs doing. A step red after three attempts with `No files were found` means the caller's files are missing, not a transport fault. A step red after three failed attempts with any other error is a fault that outlasted 30 s; read the three attempt logs before rerunning.

Known gap: run history cannot say why a connection was refused or reset, and no network or runner lifecycle telemetry exists for the two failure intervals. The retry turns one socket fault on these calls into one bounded retry instead of a failed shard and queue entry. It does not explain or remove the fault.
