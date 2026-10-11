---
title: CI result cache and in place retry
parent: Reference
nav_order: 5
---

# CI result cache and in place retry

One CI algorithm for every repo and workflow: a check whose real inputs did not change reuses its last green result, and a check that fails is retried in place a bounded number of times inside the same job. No run is ever restarted from zero because one test flaked.

Status: design. Issue [#2365](https://github.com/the-cloud-clockwork/agentihooks/issues/2365). Slices are listed in [Slices](#slices); each one is a separate pull request.

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
| Helm kind due check: `kind-due` | one job | chart paths changed | path rule written by hand |
| Dev push | whole run | none | always runs everything; `ci_reuse.py` decides reuse only for `merge_group`, so the dev push reuse named in the task does not exist yet |

Nothing retries. A failed test fails its shard, the shard fails `Gate — Required`, and the only recovery is a rerun of failed jobs on a pull request or a new queue entry.

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
- Counted: every retry is a ledger line. A unit flaky on two separate runs within seven days raises a ledger follow up naming it. Retry absorbs a flake; it does not remove the follow up to fix it.
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
