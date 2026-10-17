# Tests workflow timing

## Delivery tiers

Pull request checks run once on the current head: lint, format, size, static analysis, changed-line coverage and mutation, and touched tests. The `dev` merge queue runs the full unit suite and test count checks on the merge group; it does not repeat the pull request checks. Shard time budgets do not hold the queue.

Each integration push runs the promotion tier, including module coverage, scheduled mutation reports, image and deployment smoke checks and duration refresh. A running promotion run finishes; newer heads replace only the pending promotion run. A red promotion keeps `main` unchanged while `dev` keeps merging, and the swarm promotion watch files repair work. Green promotion lets CI alone merge the graded `dev` head into `main`, tag a release and publish versioned and `:release` images. The `:dev` image remains an experimentation image.

See [Delivery tiers](/docs/reference/delivery-tiers/) for check ownership. The measurements below record earlier workflow versions; their old event assignments do not describe the current tiers. Tests and mutation runs execute in CI only.

## Proof dispatch lanes

Browser jobs share the [verified Chromium cache](reference/browser-cache.md): restore is attempted on every ref when no verified baked browser is available, while only `dev` pushes save after a miss and successful installation. The guide covers workflow setup, GitHub cache scope, recovery and cache inventory checks.

Proof dispatches share three hosted lanes, the concurrency groups `hosted-lane-a`, `hosted-lane-b` and `hosted-lane-c`. A run picks its lane from the last digit of its run id: 0 to 2 is lane a, 3 to 5 lane b and 6 to 9 lane c. Each lane runs one run at a time and queues the rest (`queue: max`), so at most three such runs hold hosted runners at once. The lanes are used by every `proofs.yml` dispatch except `proof=tests` (that one gets its own `focused-proof-<run id>` group), by every Mutation refinement proof run, by `workflow_dispatch` runs of the Tests, Swarm worker image and Test leaks workflows, by Worker hive pod runs other than a push to `dev`, and by the brain smoke, Helm chart in kind, CI templates, Swarm container smoke, Swarm worker image smoke and Ledger server load workflows when they run under their own name.

Branch pushes of the Startup stages and Worker resources workflows stay off the lanes. Each push resolves to a per-branch group, `startup-stages-refs/heads/<branch>` or `worker-resources-refs/heads/<branch>`, with `queue: single`: a newer push to the same branch replaces the pending one, and the push never waits behind or blocks a proof dispatch. A `workflow_dispatch` of either workflow still queues on a lane. When another workflow calls either one, the call runs under the caller's name and gets its own `startup-stages-<run id>` or `worker-resources-<run id>` group with `queue: single`.

| Run | Concurrency group | Queue |
|---|---|---|
| Startup stages, branch push | `startup-stages-refs/heads/<branch>` | single |
| Startup stages, dispatch | `hosted-lane-<a, b or c>` | max |
| Worker resources, branch push | `worker-resources-refs/heads/<branch>` | single |
| Either workflow, called by another workflow | `startup-stages-<run id>` or `worker-resources-<run id>` | single |
| Worker resources, dispatch | `hosted-lane-<a, b or c>` | max |
| `proofs.yml`, any proof but `tests` | `hosted-lane-<a, b or c>` | max |
| `proofs.yml`, `proof=tests` | `focused-proof-<run id>` | max |

Before this split, a Startup stages push measurement held a lane for minutes and every proof dispatch hashed to that lane waited behind it: a push created at 03:55:26 queued on lane a until 04:21:36 and held it until 04:27:39, and a swarm worker image dispatch on the same lane, created at 04:06:34, never got a job and was cancelled after 20 minutes in the queue. After it, on lane c, a Startup stages push created at 08:47:30 ran from 08:47:43 to 08:52:00, and a worker image proof dispatch created at 08:50:49, while that push was still running, started its first jobs at 08:50:50 and passed at 08:53:58 (runs 39018691198 and 39018894036, pull request #3956).

Dispatch a worker image proof on a branch:

```bash
gh workflow run proofs.yml \
  --ref <branch> \
  -f proof=worker-image
```

Check that a proof dispatch did not wait on a branch push, from the queued and started times of both runs:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/workflows/startup-stages.yml/runs?branch=<branch>&event=push" \
  --jq '.workflow_runs[] | {id, created_at, run_started_at, status}'
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs \
  --jq '[.jobs[].started_at] | min'
```

A dispatch whose first job starts within seconds of its `created_at` did not queue. If a dispatch still sits queued for minutes, list the runs on its lane: another dispatch or a lane workflow run holds it, not a Startup stages or Worker resources push. `tests/test_ci_hosted_lanes.py` checks, for a sample branch across all ten run id endings, that a branch push of either workflow never resolves to a lane while a dispatch still does.

## Codex CLI cache

The live Codex API test, `tests/routing/test_codex_api.py`, runs the real Codex CLI. `tests/shards.py` pins that file to unit shard 1, so only two Tests jobs need the CLI: the Python 3.12 shard 1 of the `unit` job, and a Python 3.12 leg of the `unit-selected` job whose selected files include that test. Both read the binary from the Actions cache and never write it.

| Job | When it needs the CLI | Cache action | On a miss |
|---|---|---|---|
| `unit` | Python 3.12 shard 1, when the run does not take its results from an earlier run (`needs.split.outputs.source` empty) | `actions/cache/restore` | downloads the release and runs it; saves nothing |
| `unit-selected` | Python 3.12 leg whose files name `tests/routing/test_codex_api.py` | `actions/cache/restore` | downloads the release and runs it; saves nothing |
| Codex cache workflow `seed` | trusted actors, on the self-hosted pool | `actions/cache` | downloads the release; the post step saves it on the ref that ran the seed |

When the CLI step runs, the job puts `~/codex` on `PATH` and sets `CODEX_CLI_REQUIRED=1`, so a missing CLI fails the test instead of skipping it. A `unit` run that takes its results from an earlier run sets neither, and the test is not run there.

The cache key is `codex-<runner OS>-<CODEX_RELEASE>`, today `codex-Linux-rust-v0.160.0`, and the path is `~/codex`. The binary is the `codex-x86_64-unknown-linux-musl` asset of that release from `github.com/openai/codex`; the cache entry is 109,304,501 bytes (about 104 MiB) in the cache list.

Only the Codex cache workflow (`codex-cache.yml`) saves the entry. It runs on every push to `main`, every six hours (`23 */6 * * *`) and on dispatch, and saves on the ref it runs on: `main` for pushes, the schedule and `gh workflow run codex-cache.yml --ref main`. A cache entry is visible to runs of the ref that saved it and of the default branch, `main`. Merge queue refs (`gh-readonly-queue/dev/...`), pull request refs and `dev` therefore all restore the one `main` entry, and no merge queue branch stores its own copy. Before this workflow, the unit shard saved a copy on each ref it ran on; merge queue refs could not read the `dev` copy, so a queue run logged `Cache not found` and spent 220 seconds in `Install the Codex CLI` (run 38802694821).

On an exact key hit `actions/cache` restores the entry and skips its save. Each scheduled or push restore updates the entry's last access time, which keeps it inside the seven day eviction window for unused caches between promotions.

The seed job saves only on the self-hosted pool the unit shards restore on. The self-hosted unit runners save gzip caches and hosted runners save zstd caches, and the two produce different cache versions, so a hosted save would not be restorable by the unit shards. The proof seed run 38808361430 on the self-hosted pool saved version `9275f631…`, the same version the shards restore. When the trusted actor check routes the job to `ubuntu-latest`, the job warns `Only the self-hosted pool unit shards restore on may save the Codex cache.`, skips every other step and still passes.

### Changing the Codex release

`CODEX_RELEASE` is set in three places: the `unit` and `unit-selected` job environments in `test.yml` and the `seed` job in `codex-cache.yml`. Change all three in one pull request; `tests/test_ci_workflow.py` fails when they differ. The new key does not exist until the next push to `main` or a dispatch of the seed:

```bash
gh workflow run codex-cache.yml --ref main
```

Until then the Codex shard downloads the release on every run, which still passes.

### Checking the cache

List the Codex entries with their ref and size:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/caches?key=codex-" \
  --jq '.actions_caches[] | [.key, .ref, .size_in_bytes, .last_accessed_at] | @tsv'
```

Expect one entry on `refs/heads/main` per release. An entry on another ref was saved by the unit shard before the seed workflow existed, or by a seed dispatched on that ref; nothing refreshes it and it expires seven days after its last use. On 2026-10-17 the list showed the `main` entry and one `dev` entry created on 2026-10-13, before the seed workflow merged on 2026-10-15.

List the seed runs:

```bash
gh run list --workflow codex-cache.yml --limit 5 \
  --json event,headBranch,conclusion,databaseId
```

| Symptom | Cause | Action |
|---|---|---|
| `Install the Codex CLI` runs in the Codex shard | the restore missed: no entry for the current `CODEX_RELEASE` on `main` | dispatch the seed on `main`, then check its run |
| Seed run passes with the `Only the self-hosted pool ...` warning | the run was routed to `ubuntu-latest` and saved nothing | dispatch it again as a trusted actor |
| `CODEX_RELEASE` test failure in `tests/test_ci_workflow.py` | the three release values differ | set the same release in all three jobs |
| `Install the Codex CLI` step fails | the release download failed; the job stops before the test runs | read the step log for the `curl` error and rerun |
| `test_codex_api.py` fails rather than skips | `CODEX_CLI_REQUIRED=1` is set but `codex` is not on `PATH` | check that the `Use the Codex CLI` step ran and added `~/codex` to `PATH` |

## Unit checkout

Each `unit` shard checks out the graded commit as a partial clone: `ref` set to the run's commit SHA (`github.sha`), `fetch-depth: 0` (full history) and `filter: blob:none` (no file contents from old commits). The initial `git fetch` downloads commits and trees only. `git checkout` then fetches every file blob of the graded tree from the promisor remote in one batch, about 4400 blobs and 45 MB.

`actions/checkout` retries `git fetch` but not `git checkout`. Before the change, one silent connection during that lazy blob fetch held unit (3.12, 9) of Tests run 39010480846 for 617 seconds before `curl 56 Recv failure: Connection timed out` failed the step, and the shard ran no tests (issue #4063). Over 263 Tests runs from 02:24 to 12:32 UTC on 17 October 2026, 4734 unit checkouts had a p50 of 4 seconds and a p99.9 of 10 seconds; that one was the only stall.

The checkout now runs as two steps:

| Step | Runs when | On failure |
|---|---|---|
| `Check out the graded commit` (`id: checkout`) | always, first step of the job | `continue-on-error: true`: the step shows as failed and the job continues |
| `Check out the graded commit, attempt 2` | `steps.checkout.outcome == 'failure'` | the job fails; no tests run and shard-check reports the shard's tests as missing |

Both steps use the same `with` block and set:

```yaml
env:
  GIT_HTTP_LOW_SPEED_LIMIT: "1000"
  GIT_HTTP_LOW_SPEED_TIME: "60"
```

Git aborts any HTTP transfer that stays below 1000 bytes per second for 60 seconds, so a stalled lazy blob fetch fails in about a minute instead of about ten. The variables cover every HTTP transfer in the step, the initial `git fetch` included. The workflow test pins both values as literals, so changing one means changing the test with it. Attempt 2 runs on the same runner with the same commit, history depth, filter and shard, so a retried shard grades the same tests as one that passed on attempt 1. The retry runs once; a second stall fails the job.

`tests/test_ci_workflow.py` fails if the two checkouts are not the first two steps of `unit`, if either loses the low speed settings or the `with` block above, if the first step drops `continue-on-error` or gains an `if`, or if the second step runs on anything but the first step's failure.

### Proof

| Run | Result |
|---|---|
| Tests 39037534575, tests only head | the new workflow test failed, nothing else |
| Plant run 39037894054, a CONNECT proxy that serves the initial fetch and goes silent during the lazy blob fetch | the old checkout hung until the step's 5 minute cap; the new checkout aborted 66 seconds after the stall, on the same promisor object as the incident, and attempt 2 passed in 5 seconds with the graded commit, full history and partial clone |
| Same commit, cold, three runs each | `blob:none`: median 5.0 seconds and 21 MB of `.git`; full blobs: median 6.6 seconds and 71 MB |
| Tests 39038020220 on the fixed head | all 18 unit shards passed on attempt 1 |

The partial clone stays: a full blob checkout took longer and stored more than three times the `.git` bytes.

### Checking a run

List the unit shards of a run whose second checkout ran:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs?per_page=100" \
  --jq '.jobs[] | select(.name | startswith("unit (")) | {name, attempt2: (.steps[] | select(.name == "Check out the graded commit, attempt 2") | .conclusion)} | select(.attempt2 != "skipped")'
```

A shard listed with `success` stalled once and recovered; one listed with `failure` failed both attempts. In the log, a stalled first step ends with `RPC failed; curl 28 Operation too slow. Less than 1000 bytes/sec transferred the last 60 seconds`, then `fatal: could not fetch <object> from promisor remote`. Attempt 2 then warns `Unable to clean or reset the repository. The repository will be recreated instead.` and checks out from scratch.

| Symptom | Cause | Action |
|---|---|---|
| First checkout failed, attempt 2 passed, tests ran | one stalled lazy blob fetch, recovered | none; the shard's results stand |
| Both checkouts failed, shard-check reports missing tests | the stall or a GitHub outage persisted past the retry | read both step logs; rerun the failed jobs once GitHub serves fetches again |
| A checkout step runs for minutes with no abort | the low speed settings were removed from that step | restore the `env` block; `tests/test_ci_workflow.py` fails without it |
| A checkout fails within seconds on both attempts | the error is not a stall: a bad ref, permissions or a missing commit | read the first error in the step log; a retry does not fix it |

## Historical measurements

Last ten runs of the Tests workflow on dev (35380163028, 35379942561, 35379468297, 35156774129, 35156548701, 35156299806, 35155214526, 35138446531, 35138276256, 35137837913). Seconds per step, mean and max over those runs.

| Job | Step | Mean | Max | Slowest run |
|---|---|---|---|---|
| unit (3.12) | Run tests | 39.6 | 41 | 35155214526 |
| unit (3.11) | Run tests | 34.1 | 41 | 35156774129 |
| unit (3.11) | Install dependencies | 19.0 | 34 | 35138446531 |
| unit (3.12) | Install dependencies | 18.1 | 26 | 35156548701 |
| lint | Install ruff | 2.0 | 4 | 35137837913 |

Slowest step: Run tests in the unit job. The two matrix jobs already run in parallel, and lint finishes in under 10 seconds. Dependency install is the largest remaining cost and is recorded as ledger task ci-pip-cache.

Run tests took 55 to 84 seconds per matrix job on a single process. The Tests workflow now runs pytest with `-n auto` (pytest-xdist); locally four workers take the suite from 82 to 34 seconds. Re-measure this table after the first runs on dev.

The unit job now runs Python 3.11 as six shards and Python 3.12, which measures coverage, as twelve shards, one runner each (pytest-split, `--splitting-algorithm least_duration`), each shard still on `-n auto`. Shards are balanced from `.test_durations`; refresh it with `python -m tests.refresh_durations`, which runs the suite five times and stores each test's median. A single `--store-durations` run is too noisy: against two held-out runs it split the shards 4.4 to 9.6 seconds of test time, the five-run median 7.7 to 8.9. A test fails once a tenth of the suite has no stored duration. Locally the four shards take 8.6 to 9.9 seconds each with two workers.

Each unit shard stores the durations it measured (`--store-durations`) and uploads them as the `durations-<python>-<shard>` artifact. `python -m tests.refresh_durations --ci 3` downloads the artifacts of the last three runs that kept them and stores each test's median, so the shards balance on CI times; local times differ from CI per test (a fakeredis inbox test took 1.9 s locally and 8.6 s on CI). Each dev push refreshes them with `--ci-run <passed run> --ci 5`: the median over the run that passed the tree and the newest runs that kept durations, for the tests that run measured, since one run's times carry each shard's runner speed.

Coverage was most of each shard's Run tests time. Only the 3.12 shards measure it now, with `COVERAGE_CORE=sysmon`; the 3.11 shards run without it. Run tests per shard went from 26 to 35 seconds (run 37239540788) to 13 to 18 seconds on 3.11 and 16 to 26 seconds on 3.12 (run 37239888769).

The Tests workflow no longer measures coverage on any shard; Sonar measures it in its own workflow. Before the change the slowest 3.12 shard ran 21 to 26 seconds against 15 to 22 seconds for the slowest 3.11 shard (runs 37241498657, 37241419135, 37241352383).

A dev push runs the whole suite itself, with no GitHub API call. Skipping a tree its pull request already passed needed an artifact search through the API, whose hourly quota every run of the repository shares; once that quota ran out the lookup failed, the push ran the suite anyway and the durations refresh failed before it could save the cache. The push run now hands its own shard durations and coverage to the refresh and Sonar jobs through artifacts of the same run, and the refresh keeps the last five runs' samples in the Actions cache. Every shard must split on the same durations, so one `durations` job looks the base revision's cache key up once and each shard restores exactly that key, or nothing when the lookup found none; a dev push run that saves the key while the shards start can no longer give two shards different splits. A dev push whose commit dev already moved past skips its Sonar analysis, read from `git ls-remote`, and leaves it to the newer run. Merge queue commits already passed the Sonar quality gate in their queue run; a direct operator push that dev moves past before its Sonar check runs gets no analysis of its own, only the newer run's analysis of a state that contains it.

Each unit shard now collects only its own test files. `--shard N/M` (`tests/conftest.py`) assigns every `tests/**/test_*.py` file to one shard, balanced on its summed `.test_durations`, and ignores the rest at collection, so no xdist worker imports and collects the whole suite to keep a quarter of it. pytest-split stays only for `--store-durations`. Locally on four pinned CPUs with cold bytecode, the slowest shard went from 6.6 to 4.5 seconds.

The shard split also weighs each test file's source size (`SECONDS_PER_SOURCE_BYTE` in `tests/shards.py`): every worker of a shard rewrites the asserts of every file it collects, while the stored durations are split between the workers. The two installer test files, the largest in the suite, no longer share a shard. Locally with cold bytecode the slowest shard's collection went from 0.78–0.81 to 0.65–0.67 seconds over three runs.

Before its xdist workers start, the controller of a sharded run forks one child per worker (`warm_imports` in `tests/shards.py`, started from `pytest_configure` in `tests/conftest.py`). Each child imports every Nth test module of the shard and exits, which leaves the assert-rewritten bytecode of the test files and the compiled bytecode of what they import in `__pycache__`. The workers then load that bytecode instead of each rewriting and compiling the whole shard on the fresh runner. The Run tests step sets `PYTHONUNBUFFERED=1` so its log stamps the session header, the collected item count and the first result when they happen. Mean time from the step start to the first test result across the eight shards went from 3.43 and 3.09 seconds (run 37247388703, two attempts) to 2.53 and 2.29 seconds (run 37248154020, two attempts); collection after the header went from 1.6–2.5 seconds to 0.5–1.6.

The Run tests step passes `-p no:` for the eight pytest plugins no test uses: anyio, pytest-split, pytest_cov, xdist.looponfail, legacypath, doctest, pastebin and junitxml. Each one cost the controller and every worker an import, from cold bytecode in the controller. On the runner, before this change, the controller of shard 3.12/1 reached the diagnostic plugin 0.57 seconds after the step start (importing pytest), configure 0.48 seconds later (entry point plugins and conftest) and the session header 0.19 seconds after that (run 37249022827, `-X importtime` and per hook stamps). Locally on four pinned CPUs with cold bytecode the header went from 0.41–0.55 to 0.31–0.40 seconds. On the runner the mean time from the step start to the header over the eight shards went from 1.11–1.46 seconds (runs 37248611179, 37248985412, 37249074263, 37249312386) to 1.05 and 1.07 (runs 37249499663, 37249585739), and to the first test result from 2.34–2.76 to 2.35 and 2.33. Compiling the repo sources in the background during the controller start and running the warm up children at a lower priority gave no gain locally and are not used.

xdist sets up its workers one at a time: each execnet gateway's bootstrap handshake finishes before the next worker process starts. The controller of a sharded run now replaces `NodeManager.setup_nodes` with `setup_nodes_in_parallel` (`tests/shards.py`), which sets up all workers in a thread pool. On the runner the four worker processes of every shard now start within 0.05 seconds of the controller's session start (run 37250393606, a temporary stamp plugin), where the last used to start up to half a second after the first. Each worker then still needs 0.33–0.77 seconds to reach configure and 0.3–0.8 seconds more to collect, so the mean time from the session header to the workers being ready went only from 0.95–1.17 seconds (runs 37250078904, 37250040471, 37249965394, 37249767275) to 0.93, 0.95 and 1.06 (run 37250137542, three attempts).

The 3.12 shards measure coverage again and were the slowest unit jobs: 209 to 226 seconds against 120 to 129 seconds for the slowest 3.11 shard (dev push runs 37755634341, 37755213262, 37754753281). Python 3.12 has no low overhead core for branch coverage, so 3.12 now runs twelve shards and 3.11 runs six. On the Anton pool the 3.12 shards summed up to 1061.7 seconds of test time against the 900 second shard budget at eight shards (run 38305119360), so they split twelve ways, and the four 3.11 shards up to 964.0 seconds (run 38308749146), so they split six ways.
