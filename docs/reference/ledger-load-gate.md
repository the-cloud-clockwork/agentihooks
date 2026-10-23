---
title: Ledger load gate
parent: Reference
nav_order: 27
---

# Ledger load gate

The ledger load gate starts a scratch ledger server holding a ledger the size of the live one, drives ninety seconds of writes and reads from ten clients, and fails when the server is too slow, too busy or keeps growing. A write latency check alone can pass a server whose thread count or memory grows steadily, and such a server stalls the live swarm once the growth adds up. The gate therefore also samples the server's thread count and resident memory through the whole run and fails when either keeps rising past a limit set in code.

The gate is `tests.ledger_load.gate`. The `Ledger server load` workflow runs it.

## Where it runs

| Event | Runs | Tier |
|---|---|---|
| Push to `dev` | yes, as the `ledger-load` job of the Tests workflow | [promotion](delivery-tiers.md) |
| `workflow_dispatch` of Tests that passes admission | yes | promotion |
| Pull request and merge group | no | none |
| `workflow_dispatch` of `Ledger server load` | yes, under its own name in a [hosted lane](../ci-timing.md) | none |

`ledger-load` is a need of `Gate — Required`, so a red run holds promotion of that `dev` head to `main`; it never holds the merge queue. Per [delivery tiers](delivery-tiers.md), a red run gets a fixer task, never a rerun until it passes. The job has a fifteen minute timeout and a green run takes about two minutes and fifteen seconds, ninety seconds of which are the workload.

The job keys its declared inputs with the [CI result cache](ci-result-cache.md). When the key hits, the gate step is skipped and the earlier green result stands. Only a passing push or dispatch saves the key.

The gate refuses to run outside GitHub Actions:

```text
the ledger load gate runs in CI only: it loads the machine for a minute and a half
```

## Hierarchy CPU Redis acquisition

The `Hierarchy CPU proof` also needs Redis before it can compare the base and
head ledger workloads. An acquisition failure stops setup before any CPU or
latency result exists. Treat service readiness and workload grading as separate
results.

### Delivered service image repair

[Pull request 4743](https://github.com/the-cloud-clockwork/agentihooks/pull/4743)
applied the ledger load registry repair from
[pull request 4680](https://github.com/the-cloud-clockwork/agentihooks/pull/4680)
to the hierarchy workflow. It replaced
`mirror.gcr.io/library/redis:7-alpine` with
`public.ecr.aws/docker/library/redis:7-alpine`. Existing image pull retries
remained unchanged; the repair added no retry loop or setting.

This is the historical service configuration delivered by that change, not
the current hierarchy setup:

```yaml
services:
  redis:
    image: public.ecr.aws/docker/library/redis:7-alpine
    ports:
      - 6379:6379
    options: >-
      --health-cmd "redis-cli ping"
      --health-interval 2s
      --health-retries 15
```

The fifteen health retries check container readiness after acquisition. They
do not retry an image download. The registry replacement preserved the port,
health check, runner selection and workload assertions.

### Current setup and usage

The current hierarchy workflow starts `redis-server` directly on each probe
runner instead of pulling a service image. If Redis is already installed, it
prints its version and skips package acquisition. Otherwise it uses the Ubuntu
archive source with apt `Acquire::Retries=5` to refresh indexes and install
`redis-server`. An incomplete index refresh emits a warning; installation must
still succeed. This apt setting belongs to the later native setup, not the
service image repair.

Both hierarchy and load probe jobs start Redis with persistence disabled:

```bash
redis-server --port 6379 --save '' --appendonly no --daemonize yes
```

They require `redis-cli -p 6379 ping` to return `PONG`, trying up to thirty
times with one second between attempts, then fail if Redis is not ready.
Cleanup runs `redis-cli -p 6379 shutdown nosave`. There is no workflow input
for the Redis image, port or retry counts; those settings live in the workflow.

Run the proof in CI on the branch to be measured, then inspect its setup and
grading steps:

```bash
gh workflow run hierarchy-cpu.yml --ref <branch> -f base=<base-revision>
gh run list --workflow hierarchy-cpu.yml --branch <branch> --limit 5
gh run view <run-id> --log
```

The optional `base` selects the comparison revision. On a manual dispatch with
no base, hierarchy uses the merge base with `dev`; load uses its pinned baseline.
Use an explicit base when reproducing a historical comparison. Do not run the
load proof on the shared development host.

### Acquisition evidence and troubleshooting

| Observation | Meaning and action |
|---|---|
| Historical `Initialize containers` fails with missing attestation blobs | Acquisition failed before the workload. Read the image name and pull errors; changing health retries cannot repair missing registry blobs. |
| Current `Start Redis on this runner` fails during apt installation | Read the archive and package errors. This setup no longer acquires a Redis container image. |
| Redis starts but the ping loop fails | Check the startup error and port availability on the CI runner before interpreting workload results. |
| Redis is ready but the comparison fails | Inspect the measured CPU, latency or response differences. Successful acquisition alone does not pass the workflow. |

The [ledger promotion failure](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39206814774/job/117913406677)
exhausted three pulls of the Google mirror image in about fifty seconds. It
established exposure to the same image, not an observed hierarchy failure count.
The [hierarchy proof on the repaired head](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39229149347/job/117988597865)
initialized Redis from public ECR in six seconds with zero image pull failures.
Its response identity and CPU per read check passed, but its paired write latency
and CPU comparison failed. This proves the acquisition repair on that head;
it does not establish a fully green hierarchy workflow or a measured reduction
in hierarchy image pull failures across a time window.

## Swarm smoke Redis acquisition

The `Swarm container smoke` workflow, `.github/workflows/swarm-smoke.yml`,
starts Redis 7 and Redis 7 Alpine containers in its scenarios. It runs as the
`swarm-image` job of the Tests workflow on every push to `dev` and on admitted
Tests dispatches, where it is a need of `Gate — Required` and so holds
[promotion](delivery-tiers.md) when red. `proofs.yml` also calls it, and a
dispatch under its own name runs in a [hosted lane](../ci-timing.md).

### Registry and retries

[Pull request 4844](https://github.com/the-cloud-clockwork/agentihooks/pull/4844)
moved both Redis pulls from the Google mirror to public ECR, the registry the
ledger load Redis service (`public.ecr.aws/docker/library/redis:7-alpine`) and
the hierarchy CPU repair use. The pulls run in the `Build the swarm image` step,
in parallel with the image build:

```bash
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:7 &&
  docker tag public.ecr.aws/docker/library/redis:7 mirror.gcr.io/library/redis:7 &
redis=$!
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:7-alpine &&
  docker tag public.ecr.aws/docker/library/redis:7-alpine mirror.gcr.io/library/redis:7-alpine || status=$?
wait "$redis" || status=$?
wait "$build" || status=$?
exit "$status"
```

Each pull goes through the existing `scripts/packaging/pull-runtime-image.sh`;
the change added no retry loop or setting. That script makes up to three
attempts per image. Each attempt reads the image manifest, picks the
`linux/amd64` digest and pulls that digest, with each command bounded by
`PULL_ATTEMPT_SECONDS` (default 120). A failed attempt prints
`Pull of <image> failed on attempt <n> of 3; retrying in 10 seconds` and waits
ten seconds; the third failure exits with the pull's status.

Each `docker tag` runs only after its own pull succeeds, so a failed pull never
leaves a mirror named image behind and the step exits non zero once the build
and both pulls have finished.

### Mirror names in the scenarios

The tag gives each image the `mirror.gcr.io` name the scenarios already use, so
scenario scripts, `compose.yaml` and user facing defaults are unchanged:

| Scenario | Image name it starts | Start |
|---|---|---|
| `hive-launch-smoke.sh` | `mirror.gcr.io/library/redis:7` | `docker compose up --pull never` |
| `swarm-smoke.sh` | `mirror.gcr.io/library/redis:7` (from `compose.yaml`) | `docker compose up --pull never` |
| `compose-hive-smoke.sh` | `mirror.gcr.io/library/redis:7` | `docker compose up --pull never` |
| `swarm-init-smoke.sh` | `mirror.gcr.io/library/redis:7` | `swarm init` from the installed package |
| `hive-join-smoke.sh` | `mirror.gcr.io/library/redis:7-alpine` | `docker run --pull never` |

Each scenario still calls `pull-runtime-image.sh` with the mirror name before it
starts Redis. The workflow sets `SWARM_SMOKE_RUNTIME_IMAGES_READY=true` on the
scenario steps; with it set, the script exits at once for either Redis mirror
name when that image is already present locally as `amd64`, so CI makes no
request to the Google mirror. Without the variable, for example when a scenario
runs on a workstation, the script pulls the mirror name as before.

### Steps after a failed pull

Since [pull request 5000](https://github.com/the-cloud-clockwork/agentihooks/pull/5000),
every later step that builds on the image runs only when
`!cancelled() && steps.build.outcome == 'success'`: the four parallel scenarios, swarm init,
Python setup and the terminal clients check. A failed Redis pull fails the
build step, those steps are skipped and the job ends red at once. The
attestation proof step runs only on `workflow_dispatch` and does not depend on
the build.

### Running and inspecting

Dispatch the workflow on a branch and read the pull lines from its log:

```bash
gh workflow run swarm-smoke.yml --ref "<branch>"
gh run list --workflow swarm-smoke.yml --branch "<branch>" --limit 5
gh run view "<run-id>" --log | grep -E 'public.ecr.aws/docker/library/redis|Pull of .* failed'
```

A healthy run shows a digest pulled from `public.ecr.aws/docker/library/redis`
for both tags and no `Pull of` retry lines.

`tests/test_ci_smoke_acquisition.py` guards the contract in CI:
`test_smoke_pulls_redis_from_the_ledger_load_registry` reads the registry from
the ledger load Redis service and requires both smoke pulls to use it and no
smoke pull to name the Google mirror. To move Redis to another registry, change
the ledger load service and the smoke pulls in the same pull request.
`test_smoke_steps_after_the_build_run_only_when_the_build_and_redis_pulls_succeeded`
guards the step conditions.

### Troubleshooting

| Observation | Meaning and action |
|---|---|
| `Pull of public.ecr.aws/docker/library/redis:<tag> failed on attempt 3 of 3` | Public ECR did not serve the image within three bounded attempts. Read the earlier attempt errors; the scenarios were skipped, so no scenario result exists for that run. |
| A scenario fails because `mirror.gcr.io/library/redis:<tag>` is missing locally | The tag step did not run. Check the build step for a failed pull, or a scenario run outside the workflow without a prior pull. |
| The log shows a pull from `mirror.gcr.io/library/redis` | The scenario ran without `SWARM_SMOKE_RUNTIME_IMAGES_READY=true`, or the local image was not `amd64`. |
| Redis pulls succeeded but a scenario failed | Acquisition is not the cause; read that scenario's own output. |

### Evidence

- Red: [Tests run 39291414278](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39291414278/job/118193570242)
  on the tests only commit failed only the new contract test, because the
  workflow still pulled `mirror.gcr.io/library/redis:7`.
- Green: [swarm smoke run 39292084539](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39292084539)
  on the branch pulled both Redis digests from public ECR and passed every
  scenario step.
- Number: across the 78 swarm smoke jobs on `dev` pushes on 19 October, failed
  Redis pull attempts were 0. The three of three failures named in the follow up
  came from the ledger load Redis service in
  [run 39206814774](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39206814774/job/117913406677),
  so this change aligns the registry with ledger load rather than repairing a
  measured swarm smoke failure.

## Hierarchy paired load false reds

On 19 October the `Hierarchy CPU proof` went red on
[pull request 4743](https://github.com/the-cloud-clockwork/agentihooks/pull/4743),
which changed only the workflow file. Its paired load step reported head write
latency 22 percent and CPU 24 percent above base. Swarm task t467 and
[issue 4818](https://github.com/the-cloud-clockwork/agentihooks/issues/4818)
found the cause: the paired comparison margin was tighter than the noise of the
self-hosted runners. Dev had no write regression and the base and head did not
run on different nodes.

### The grading that failed

At the time the load step ran base and head gates interleaved twice in one job
on one `anton-oss` runner (base, head, base, head). `tests.ledger_load.paired`
then took the best write p95 and the best busiest CPU window of each side's two
runs and failed when the head exceeded the base by more than 10 percent. The
base was pinned at `726353637`.

### How each suspected cause was tested

| Suspected cause | Test | Result |
|---|---|---|
| Runner noise across nodes, or a busy node after the runner outage | Read the runner of each side from the job | Excluded. Both sides ran in the same job on the same runner, so they always shared a node |
| A write regression on dev since the pinned base | Compare current dev with a base from before and after the hosted ledger fencing merge, the suspected change | No regression. Dev against the base before fencing passed. Against the pinned base, dev read 1.18, 1.75 and 1.28 times write p95 in three runs with no trend |
| A margin too tight for paired runs | Run the comparison with identical code on both sides | Confirmed. Identical code differed 1.82 and 2.2 times in best write p95, and 1.13 to 1.15 times in the busiest CPU window, both beyond the 1.1 times margin |

### Runs that settled it

| Run | Comparison | Result |
|---|---|---|
| [39148204008](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39148204008) | Pinned base against a head that added only the hierarchy work | Green. Head matched base within 1 percent |
| [39229149347](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39229149347) | Pinned base against the head of pull request 4743 | Red. Best write p95 0.144 s against 0.099 s, busiest CPU window 0.141 against 0.127 cores |
| [39269966009](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39269966009) | Dev against a base before the fencing merge | Green. Best write p95 0.136 s on both sides; one base round read 0.407 s, three times its twin on the same runner |
| [39269961106](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39269961106) | The same dev head against a base after the fencing merge | Red at 1.45 times write p95, while write p50 differed only 4 to 6 percent |
| [39271200438](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39271200438) | Identical code on both sides | Best write p95 differed 1.82 times; it passed only because the slower side was base |
| [39271211837](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39271211837) | Identical code on both sides | Best write p95 differed 2.2 times and the busiest CPU window 1.13 times; it passed for the same reason |
| [39272472710](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39272472710), [39272482954](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39272482954) | Dev against the pinned base | Red at 1.75 and 1.28 times write p95. One head round read 0.657 s, 2.7 times its own other round |
| [39272493518](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39272493518) | The hierarchy proof dispatched on dev | Red on write p95 only, 0.168 s against 0.142 s, 1.18 times |

The verdict changed with the runner, not with the code. Best of two picks one
fast round per side, so a single fast base round decides the result. A write
p95 over about ninety writes is set by its four or five slowest writes.

### Grading after the fix

Swarm task t495 replaced the grading in
[pull request 4956](https://github.com/the-cloud-clockwork/agentihooks/pull/4956).
The current workflow grades as follows:

| Part | Current behavior |
|---|---|
| Load pairs | A five job matrix, `pair: [1, 2, 3, 4, 5]`. Each job starts one base and one head gate side by side on its own runner, base first on odd pairs and head first on even pairs |
| Load measures | Write p50 and the busiest CPU window. Write p95 stays an absolute budget inside each gate run |
| Load ratio | Per pair head over base ratios, combined as the geometric mean of the base first median and the head first median, so launch order cancels out |
| Load margin | `SAME_CODE_MARGIN: '1.25'` in the grade job |
| Hierarchy pairs | A four job matrix of hierarchy probes graded by `tests.ledger_load.hierarchy_paired` at `HIERARCHY_SAME_CODE_MARGIN: '1.3'` |
| Margin floor | Each grader reads its recorded same code runs and refuses a margin below their noise: the load grader below the widest single pair ratio in `tests/ledger_load/same_code_pairs.json`, the hierarchy grader below the widest per run geometric mean in `tests/ledger_load/hierarchy_same_code.json` |
| Load base | Still `726353637` unless the dispatch names a `base` |

Pull request 4956 recorded five of five same code runs green and a planted
0.4 s delay on every ledger write red at a 4.71 times write p50 median.

### Reproducing a same code control

Dispatch the proof with the branch head as its own base, so both sides run the
same code:

```bash
gh workflow run hierarchy-cpu.yml --ref "<branch>" -f base="$(git rev-parse "origin/<branch>")"
```

Read which runner took each pair and when it ran:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/<run-id>/jobs" --paginate \
  -q '.jobs[] | [.name, .runner_name, .started_at, .completed_at, .conclusion] | @tsv'
```

Read each side's graded measure from the pair logs. The last line starting with
`{"seconds"` in a log is the run the gate graded:

```bash
gh run download "<run-id>" --pattern 'load-pair-*' --dir load-evidence
grep -h '^{"seconds"' load-evidence/load-pair-1/base.log | tail -1
```

The grade job prints the pairs, their ratios, the margin and the same code
bound as JSON before its error lines.

### Troubleshooting a paired red

| Observation | Meaning and next step |
|---|---|
| `the margin Mx is below Bx, the widest pair ratio of the same code runs` (load) or `the margin Mx is below Bx, the widest geometric mean of the same code runs` (hierarchy) | The workflow margin is under the recorded noise. Raise the margin in the workflow, or record new same code runs if the runners changed |
| `head write p50 order balanced pair ratio R exceeds the 1.25x margin` on a head that does not touch the ledger server | Run a same code control first. If identical code also crosses the margin, the noise grew; record the control runs and judge the margin before looking for a regression |
| The same red on a head that changes server code | Compare dev against an older base in CI to bracket the merge that introduced it, then bisect the merges between |
| One pair far from the rest | Read that pair's runner and start time with the jobs command above. Each pair runs on its own runner, so one busy runner skews one pair |
| `head had N failed requests, base M` | Read the head server log in the pair artifact. Failed requests are graded apart from latency |

## Workload

| Setting | Value |
|---|---|
| Ledger | 1000 tasks, 60 phases, 800 follow ups, 500 chat lines, 50 alerts, three comments per task, one per phase, padded to at least 5,000,000 bytes |
| Writer clients | 10, together writing twice the live peak of 29 writes a minute; each write is followed by two reads |
| Item readers | 10, one single resource read a second each |
| Page stream | one server-sent event stream held open through the run |
| Duration | three bin sweeps of the server watcher: `(SWEEPS + 1) × BIN_SWEEP_EVERY × watch interval` = 3 × 15 × 2 s = 90 s |
| Sampler | once a second: server CPU seconds from `/proc/<pid>/stat`, and `Threads` and `VmRSS` from `/proc/<pid>/status` |

## Verdict

The run fails on any of these, each printed as a `::error::` line:

| Check | Limit | Message |
|---|---|---|
| Failed requests | none | `N requests failed, first: …`; a rejected write (`write rejected: …`), a dropped page stream (`event stream: …`) and a sampler error (`server sampler: …`) each count as one |
| Writes completed | 90% of expected | `N writes completed of M expected` |
| Write latency | p95 at most 2.0 s | `write p95 Xs exceeds 2.0s` |
| Server CPU | busiest 10 s window at most 1.0 core | `server CPU held X cores over 10s, budget 1.0`, or `no 10s window of server CPU was sampled` |
| Thread growth | 8 threads | `server threads kept growing: medians A, B, C; growth budget 8` |
| Memory growth | 65,536 KiB (64 MiB) | `server RSS kept growing: medians A, B, C; growth budget 65536` |
| Resource coverage | 60 s of samples after warmup | `server resource sampling did not cover 60s after warmup` |
| Single resource reads | 90% of expected | `N single resource reads completed of M expected` |
| Server log | no `Exception in thread` line | `a ledger server background thread died` |
| Alert expiry sweep | no alert left past its quiet hour | `the expiry sweep left N alerts past their quiet hour open` |

A server that exits before listening or does not listen within 60 s stops the run with `ledger server exited with N` or `ledger server did not listen on port P within 60.0s`.

When write p95 is the only failure, the gate prints two `::notice::` lines, repeats the run once on a fresh ledger and grades the repeat alone. The repeat prints its own JSON measure line, and `tests.ledger_load.paired` reads the last one. Any other failure, a resource growth failure included, is final on the first run.

## Resource growth check

The check grades the samples `(time, threads, RSS KiB)` the sampler took through the run:

1. Samples from the first 20 seconds (`RESOURCE_WARMUP_S`) are dropped, so server start up and client joins do not count as growth.
2. The remaining samples must number at least three and span at least 60 seconds (`RESOURCE_WINDOW_S`). Otherwise the run fails for missing coverage.
3. That span is cut into three equal time windows. A window with no sample fails the run with `server resource sampling missed a growth window`.
4. For threads and for RSS separately, the gate takes the median of each window. It fails when the medians rise strictly, first < middle < last, and the last exceeds the first by more than the limit: `THREAD_GROWTH` = 8 threads, `RSS_GROWTH_KIB` = 65,536 KiB.

Medians absorb short spikes. A server that grows and then settles, or that grows by less than the limit, passes. A sampler that cannot read `/proc` records a failed request, `server sampler: …`, and its missing samples also fail the coverage check.

## Limits graded from the base

Every limit is a module constant in `tests/ledger_load/gate.py`. The run reads the same constants from the base revision with `git show <base>:tests/ledger_load/gate.py` and grades with the stricter of the two:

| Constant | Stricter value |
|---|---|
| `THREAD_GROWTH`, `RSS_GROWTH_KIB`, `RESOURCE_WARMUP_S` | the smaller |
| `RESOURCE_WINDOW_S` | the larger |
| `WRITE_P95_S`, `CPU_CORES`, `CPU_WINDOW_S` | the smaller |
| `LEDGER_BYTES`, `CLIENTS`, `LIVE_PEAK_WRITES_PER_MINUTE`, `HEADROOM`, `MIN_WRITES`, `READS_PER_SECOND`, `SWEEPS` | the larger |

A change cannot loosen the limits it is graded by. Raising a growth limit takes two changes: land the higher value on `dev` first. Later runs grade against that value because `dev` is their base. A base older than the resource check, one that assigns none of the four resource constants, is graded with the initial values 20 s, 60 s, 8 threads and 65,536 KiB. A base missing any other constant fails the run with `the base gate assigns no …`.

The Tests workflow passes the push's `before` revision as the base, and on a dispatch of Tests its `base` input. A base that `git show` cannot read fails the run with `git could not show the ledger load gate at base …`.

## Output

Each run prints one JSON line with the measure, then the error lines:

```json
{"seconds": 90.0, "clients": 10, "write_every_s": 10.345, "writes": 90,
 "write_p50_s": 0.071, "write_p95_s": 0.178, "write_max_s": 0.31,
 "reads": 90, "read_p95_s": 0.05, "item_reads": 900, "item_read_p95_s": 0.02,
 "busiest_cores": 0.42, "errors": 0,
 "resources": [[1234.5, 6, 81234], [1235.5, 6, 81240], "…"],
 "budget": {"write_p95_s": 2.0, "cores": 1.0, "thread_growth": 8, "rss_growth_kib": 65536}}
```

`resources` holds every sample as `[monotonic seconds, threads, RSS KiB]`, so a failing run shows exactly when the growth started. The `budget` object and the `seconds`, `clients` and `write_every_s` values come from code; the measured values are illustrative. On a failure the gate also prints the last 4000 characters of the server log.

## Running it by hand

Dispatch the workflow on a branch, naming the base whose limits grade the run:

```bash
gh workflow run ledger-load.yml --ref <branch> -f base=origin/dev
```

| Input | Default | Effect |
|---|---|---|
| `base` | `origin/dev` | revision whose constants grade the run |
| `compare` | `false` | run base and head gates interleaved twice on one runner, then `tests.ledger_load.paired` fails when the head's best write p95 or busiest CPU window exceeds the base's by more than 10%, or the head had more failed requests. Each of the four gate runs still applies its own resource growth check |
| `streams` | `false` | compare page stream publication CPU between base and head instead of running the gate |

A dispatch never saves the input key unless the gate ran and passed.

## Diagnosing a failure

| Message | Meaning | Next step |
|---|---|---|
| `server threads kept growing` | threads rose through all three windows by more than eight | look for a thread started per request or per sweep that never ends: a `threading.Thread` without a join or exit condition, or an executor created per call |
| `server RSS kept growing` | resident memory rose through all three windows by more than 64 MiB | look for a cache, list or event buffer that only grows during writes |
| `server resource sampling did not cover 60s after warmup` | the sampler stopped early or never started | read the `server sampler:` error; a server that exited mid run also ends sampling |
| `server resource sampling missed a growth window` | a gap in sampling left one window empty | the sampler or runner stalled; the timestamps in `resources` show the gap |
| `the base gate assigns no …` | the base revision lacks a limit constant | dispatch with a base that has the gate, such as `origin/dev` |

## CI evidence

Recorded in [pull request 4042](https://github.com/the-cloud-clockwork/agentihooks/pull/4042). The planted thread ran on the `diffcheck/ci-323133-1138` branch:

| Run | Change | Result |
|---|---|---|
| [39032389271](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39032389271) | planted request thread that is started per request and never ends | red on thread growth only: medians 45, 70, 91 against a budget of 8; zero failed requests, write p95 0.245 s |
| [39035495745](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39035495745) | the merged head, unchanged server | green in 135 s: zero errors, write p95 0.178 s, threads six falling to four |
| [39031831528](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39031831528) | Proofs dispatch of the verdict unit tests before the check existed | red: five resource verdict tests failed |
| [39032187789](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39032187789) | Proofs dispatch of the verdict unit tests with the check | green: 45 passed |

The verdict logic is unit tested in `tests/ledger_load/test_gate.py`, which runs in the unit suite.

To read write timings on the live server by route, see [Ledger request metrics](ledger-request-metrics.md).
