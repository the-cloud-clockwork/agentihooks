# Tests workflow timing

## Helm kind registration to readiness

The promotion Helm chart proof checks recovery as well as initial readiness.
Its measured interval starts at `ledger and swarm kind-proof registered, no
lease bound` and ends at `controller readiness probe passed`. This includes a
deliberate controller freeze, liveness restart and release upgrade enabling the
control API. Image preparation, cluster creation and subsequent worker and
terminal proofs are outside the interval.

### Preserved checks and reduced waiting

After the initial release installation and chart tests, a background job
prepares the signing Secret, controller service account and RBAC, worker pod
policy ConfigMap and terminal CA Secrets. Setup overlaps the controller lease
and recovery checks. The proof waits for setup to succeed before the upgrade;
an error still fails the proof.

One Python process in the ledger Pod watches the lease every 0.2 seconds.
Acquisition requires the deployment's `SWARM_HIVE_ID` as owner; renewal requires
the same owner with a later `expires_at`. Each wait has a 180 second deadline.
Previously each read started a cold `kubectl exec` process, with a one second
sleep between reads. The separate post restart lease retake loop is unchanged.

The proof retains this sequence:

1. Restart the controller after registration and wait for rollout.
2. Require lease acquisition, renewal and zero controller restarts.
3. Freeze the controller in the isolated kind node, require exactly one
   liveness restart and verify lease retake.
4. Require successful background setup, upgrade with control API and worker
   settings, wait for rollout and require the Kubernetes runtime build log.
5. Verify startup and liveness are wired to `agentihooks controller live` and
   readiness to `agentihooks controller ready`. Execute all three commands in
   parallel, wait for every process and fail if any command fails.
6. Require the Pod's Kubernetes Ready condition and zero restarts on the
   upgraded controller before continuing with the remaining chart proofs.

Probe results print in startup, liveness, readiness order after all commands
finish. Printed order is not execution order. A passed readiness command alone
does not establish the subsequent Ready assertion or completion of the proof.

### Kind configuration

The checked in `deploy/helm/agentihooks-swarm/ci/kind-values.yaml` supplies this
controller timing excerpt to both release operations:

```yaml
controller:
  startupProbe:
    periodSeconds: 1
  readinessProbe:
    periodSeconds: 1
  livenessProbe:
    initialDelaySeconds: 5
    periodSeconds: 1
    timeoutSeconds: 5
    failureThreshold: 2
  terminationGracePeriodSeconds: 1
  env:
    AGENTIHOOKS_CONTROLLER_TICK_SECONDS: "2"
```

Liveness initial delay fell from ten to five seconds and its period from two
to one second. The startup probe already gates liveness with the same live
command. These are kind overrides: chart default liveness settings remain a
sixty second initial delay, ten second period, eight second timeout and failure
threshold three. This excerpt is not a complete cluster configuration.

There is no new flag or environment setting for the 0.2 second lease watch or
parallel execution. Change those through reviewed proof code; change fixture
probe cadence through kind values. This optimization requires no production
configuration change.

### Run and inspect in CI

Push the branch and dispatch the chart proof on that revision:

```bash
gh workflow run proofs.yml \
  --ref <branch> \
  -f proof=helm-kind
gh run list \
  --workflow proofs.yml \
  --branch <branch> \
  --limit 5
gh run view <run-id> \
  --log
```

The standalone workflow also accepts a dispatch:

```bash
gh workflow run helm-kind.yml \
  --ref <branch>
```

Both routes use the declared Helm input key. Inspect `Key the declared inputs`
first: a cache hit skips chart execution and supplies no new timing sample.
See [Helm kind proof input keys](reference/ci-result-cache.md#helm-kind-proof-input-keys)
for cache scope and due decisions. Run proofs in CI, not on the shared host.

For a fresh execution, subtract the registration marker timestamp from the
`controller readiness probe passed` timestamp in the raw Actions job log. Keep
the run link and revision. Separately require the later `controller with the
control API on is Ready under the health probes, restarts: 0` and final `Helm
chart kind proof passed` markers. Queue time and whole job duration measure
different intervals.

| Failure | Inspection |
|---|---|
| Lease acquisition or renewal times out | Read the last lease diagnostic, compare owner with `SWARM_HIVE_ID` and check whether expiry advanced. |
| Frozen controller does not restart exactly once | Inspect liveness settings, restart count and previous controller logs from failure cleanup. |
| Setup objects were not created | Inspect the earlier background setup command error; upgrade is refused on setup failure. |
| Runtime build log is absent | Inspect controller startup and worker settings after upgrade; rollout alone does not establish runtime construction. |
| Probe fails or uses an unexpected command | Inspect the named diagnostic and deployed probe wiring; all three commands remain mandatory. |
| Ready times out or restarts are nonzero | Inspect Pod state and controller logs; retain these assertions even when the explicit readiness command passed. |

### Delivery measurements

[Pull request #4391](https://github.com/the-cloud-clockwork/agentihooks/pull/4391)
and its [source issue](https://github.com/the-cloud-clockwork/agentihooks/issues/4389)
record intervals of 25.86 and 26.79 seconds before the change. The delivered
revision is `57c433590b7c82d7a45174eabbd8c6ec3aa5c05f`. After the change:

| Branch proof | Registration to readiness | Recorded result |
|---|---|---|
| [39099421881](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39099421881) | 20.41 seconds | Every check passed |
| [39099734177](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39099734177) | 21.05 seconds | Every check passed |

These branch samples demonstrate the delivered reduction. They are not a fixed
runtime guarantee or a post merge promotion measurement; runner load and later
proof changes can affect the interval.

## Delivery tiers

Pull request checks run once on the current head: lint, format, size, static analysis, changed-line coverage and mutation, and touched tests. The `dev` merge queue runs the full unit suite and test count checks on the merge group; it does not repeat the pull request checks. Shard time budgets do not hold the queue.

Each integration push runs the promotion tier, including module coverage, scheduled mutation reports, image and deployment smoke checks and duration refresh. A running promotion run finishes; newer heads replace only the pending promotion run. A red promotion keeps `main` unchanged while `dev` keeps merging, and the swarm promotion watch files repair work. Green promotion lets CI alone merge the graded `dev` head into `main`, tag a release and publish versioned and `:release` images. The `:dev` image remains an experimentation image.

See [Delivery tiers](/docs/reference/delivery-tiers/) for check ownership. The measurements below record earlier workflow versions; their old event assignments do not describe the current tiers. Tests and mutation runs execute in CI only.

## Resource sampling and stop condition regression checks

Two regression checks cover live process tree sampling and a pre push gate that overruns its stop condition budget. Their checks distinguish sampling leaks from inherited garbage, and condition execution time from hook startup and exit time.

### Whole process tree sampling

`test_measure_reports_the_whole_tree_of_a_live_workload` starts a real memory workload with two children, 48 MiB per child and a 1.5 second hold. It samples every 0.05 seconds with a 60 second workload timeout. The test retains the workload exit, process count, memory, CPU, storage and wall time assertions; the resource audit change does not widen those bounds.

The audit collects garbage before capturing warnings, then collects again after sampling while warning capture is active. This keeps an unclosed file inherited from earlier work out of the sampler's resource audit. A `ResourceWarning` raised during sampling still fails with its category, message and source location. The historical warning producer was not established by this fix.

`test_live_workload_resource_audit_owns_only_sampling_leaks` disables automatic garbage collection and test reruns for two controls: an inherited cyclic unclosed file must pass the audit, while a new cyclic unclosed file created during sampling must fail it with diagnostics. These controls verify warning ownership without replacing the real child processes with mocks.

### Pre push overrun timing

`test_a_pre_push_gate_that_overruns_is_stopped_by_the_gate_inside_the_stop_condition` sets `CONDITIONS_TIMEOUT_SEC=8` in its isolated fixture. The stop gate keeps a 3.5 second reserve, leaving 4.5 seconds for the pre push gate instead of the former 2.5 seconds at a six second timeout.

The fixture's condition shim records monotonic start and finish times to the file named by `CONDITION_CLOCK`. The assertion requires that condition process to finish within eight seconds. Interpreter startup and exit for the enclosing `python -m hooks` invocation are outside this measured interval. The test still requires exit code 2, the pre push timeout diagnostic and an empty remote head: the stop hook must not push unfinished work. If the outer runner kills the condition before it can report the gate timeout, that is a test failure.

| Setting | Scope | Meaning |
|---|---|---|
| `CONDITIONS_TIMEOUT_SEC=8` | Overrun test fixture | Condition timeout used by this regression; the documented runtime default remains `10` seconds |
| `CONDITION_CLOCK` | Test shim only | Temporary output file holding two monotonic timestamps; not a production hook setting |
| `0.05`, `60` | Sampling test arguments | Sample interval and workload timeout in seconds; not new environment variables |

See [condition configuration](hooks/conditions.md#configuration) for runtime timeout configuration. These changes require no operator configuration and do not change production sampling or stop gate behavior.

### Verify in CI

Push the branch, then run the two test files through the existing focused proof workflow:

```bash
gh workflow run proofs.yml \
  --ref <branch> \
  -f proof=tests \
  -f tests='tests/test_swarm_v2_resource_sampling.py tests/gates/test_push_stop_executor.py'
gh run list \
  --workflow proofs.yml \
  --branch <branch> \
  --limit 5
gh run view <run id> \
  --log
```

Inspect each named test's result and any `RERUN` or flaky test report. A green run after a retry does not establish a first attempt pass. The sampling ownership controls explicitly set `reruns=0`; that setting does not disable retries for every test in the selection. Tests run in CI, not on the shared development host.

If the sampling audit fails, use its warning category, message and source location to distinguish a new sampling leak from inherited garbage; retain the post sampling collection that exposes new leaks. If the overrun check fails, inspect the stop diagnostic and recorded condition interval before changing any budget. Timing the enclosing hook process against the condition timeout reintroduces the startup and exit overhead mismatch.

[Pull request #2808](https://github.com/the-cloud-clockwork/agentihooks/pull/2808) introduced the resource audit isolation. Its [green Tests run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38243723120) records both ownership controls passing on Python 3.11 and 3.12 with reruns disabled. [Pull request #4083](https://github.com/the-cloud-clockwork/agentihooks/pull/4083) changed only the overrun test timing; its [Python 3.12 selected tests job](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39051257730/job/117450548906) passed all 62 tests, including the overrun check, without a test rerun. That pull request also reports no failed or rerun line for either target test across about 165 recent dev push runs. This is historical delivery evidence, not a guarantee that future runs cannot fail.

## Standing CI budget report

The swarm's CI budget pass updates one standing health finding,
`bottleneck-stage/ci_overrun`, for pull request Tests runs that take more than
fifteen minutes from push to `Gate Required`. It does not create a follow up for
each slow run or restore hourly follow ups. Multiple slow stages appear as
evidence on that same finding, rather than as separate findings or tasks.

### Read the report

Open the watched swarm's ledger, select the Swarm tab and inspect Swarm health.
The finding includes the run overrun, its slowest stage, and a summary for every
known stage that exceeded its budget in the retained slow runs. For example:

```text
mutation: 2 runs over budget; worst 15 minutes.
lint: 1 run over budget; worst 5 minutes.
```

These counts are per stage: a run can count toward both lines. They are distinct
from the finding's `Fired N times` counter. Only stages strictly above a declared
budget are grouped; a stage exactly at its budget, an unknown stage, and
`gate-required` itself are excluded. The current mutation budget is eight
minutes and lint budget four minutes. A run can exceed the overall fifteen
minute budget without any individual stage exceeding its own budget.

Each retained slow run has a `Slow run <run id>` link beneath the evidence. Open
it to inspect the Actions run and its jobs. The report prefers the run URL and
falls back to the completed gate job URL when the run has no URL. Links open in
a new tab. The page renders only HTTP or HTTPS links, with `rel="noopener"`.

The finding carries links separately from its prose, using this shape:

```json
{
  "links": [
    {
      "label": "Slow run 39024469354",
      "url": "https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39024469354"
    }
  ]
}
```

The URL above illustrates the field format using a delivered proof run; it is
not a claim that that proof exceeded the budget. A URL pasted into evidence
text does not create the report's run anchor. Findings without structured links
retain their existing display.

Inspect the watched swarm from the command line:

```bash
agentihooks swarm <swarm> status --json
```

Use the run id from a link to inspect its result and job timings:

```bash
gh run view <run-id> \
  --repo the-cloud-clockwork/agentihooks \
  --json url,status,conclusion
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run-id>/jobs \
  --paginate \
  --jq '.jobs[] | {name, started_at, completed_at, conclusion}'
```

### Configuration and retention

The pass refreshes every five minutes and retains slow-run samples for six
hours, dated by the gate's completion time. It stores each run by its id in the
watched swarm's `ci-budget:stages` cache. Reprocessing the same run does not add
another sample. On a new sample update, runs at least six hours old are removed
from the cache and the evidence and links are rebuilt from retained samples.
The cache itself also has a six hour expiry.

The overall run budget, stage budgets, refresh interval and sample window are
code constants (`RUN_BUDGET_S`, `BUDGETS`, `REFRESH_MS` and `WINDOW_S`), rather
than new CLI flags or environment settings. Change them through the normal
reviewed code workflow. For example, a proposed mutation budget of ten minutes
would change the `mutation` entry in `BUDGETS` from `480` to `600` seconds; the
overall run threshold would remain `900` seconds unless separately changed.

The cache and standing finding belong to the watched swarm. Linking a Doctor
ledger does not redirect or duplicate the report into that ledger. Inspect the
watched swarm's health panel even when a Doctor is observing it. Existing
[standing finding verdict and expiry rules](pillars/swarm.md#commands)
still apply; sample retention does not bypass a verdict cooldown or keep a
stale finding visible indefinitely.

### Troubleshooting and delivery evidence

| Symptom | Check |
|---|---|
| Several stages are slow but only one finding appears | Expected: read the per-stage evidence on `ci_overrun`. |
| A stage has no summary line | Confirm that the run exceeded fifteen minutes, the stage has a declared budget, and its duration was strictly above it. |
| Evidence shows a URL but no clickable run link | Check that the finding has a `links` entry with `label` and an HTTP or HTTPS `url`; plain evidence text is not the link field. |
| An older run disappears from the report | Check the six hour sample window and the standing finding's expiry and verdict rules. |
| The Doctor ledger has no report | Inspect the watched swarm; the report deliberately writes only there. |

[Delivered pull request #4012](https://github.com/the-cloud-clockwork/agentihooks/pull/4012)
records red CI proofs for missing grouped evidence and the missing run anchor,
followed by a [successful focused proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39024469354)
and [successful mutation preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39026092462).
The delivered regression cases cover per-stage counts and worst times, run URL
selection, repeated-run stability, sample expiry and isolation from the Doctor
ledger. These are reporting checks, not evidence that every CI run meets the
fifteen minute target.

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

## Focused proof CPU worker limits

The `tests` proof in the Proofs workflow runs selected tests with the same CPU quota helper as the unit shards. A runner limited to four CPUs on a sixteen core node starts at most four pytest workers. Previously, `-n auto` could start sixteen competing workers on that runner.

### Dispatch selected tests

Push the branch first, then dispatch explicit test paths or pytest node ids under `tests/`:

```bash
gh workflow run proofs.yml \
  --ref <branch> \
  -f proof=tests \
  -f tests='tests/test_ci_proofs.py'
```

For browser tests, add `-f browser=true` to install Chromium before pytest. Missing requested files fail by default; `-f existing_only=true` explicitly skips missing files with warnings, but still fails if none exist. Give paths on one line without `..`. The job uses Python 3.12 and has a fifteen minute timeout. Keep the selection focused; the CPU cap does not extend that deadline.

### Automatic worker selection

The Run selected tests step resolves the worker count before starting pytest:

```bash
workers=$(python -m scripts.ci_cpu_quota) && python -m pytest "${paths[@]}" -v -n "$workers" --dist loadgroup --tb=short
```

`paths` is the workflow's array of validated test selections. The helper reads the process CPU affinity and finite cgroup CPU quotas, including parent cgroups, supporting both cgroup v2 `cpu.max` and v1 CPU quota and period files. It takes the smallest finite quota, truncates fractional CPUs to an integer, caps that at the affinity CPU count and keeps at least one worker. If no finite quota is found, it uses the affinity count, with the same minimum of one.

| Affinity CPUs | Smallest finite quota | Workers |
|---|---|---|
| 16 | 4 | 4 |
| 2 | 4 | 2 |
| 16 | 2.5 | 2 |
| 16 | 0.5 | 1 |
| 4 | none | 4 |

There is no worker count dispatch input or environment override in this helper. Configure the runner's CPU quota or affinity to change the automatic result. A CPU request alone is not a finite cgroup quota. The cap applies to the selected tests job; it does not impose a worker count on other proof types or limit subprocesses a test starts. If the helper exits unsuccessfully, `&&` prevents pytest from starting.

### Inspect the result

Open the dispatched run and its Run selected tests log:

```bash
gh run list \
  --workflow proofs.yml \
  --branch <branch> \
  --limit 5
gh run view <run id> \
  --log
```

The helper prints the worker count to stdout for pytest and a diagnostic to stderr, visible in the job log:

```text
pytest workers 4: cpu quota 4.0, affinity 4
```

If the count exceeds the intended runner allocation, check the reported quota and affinity. A quota of `None` means the helper found no finite quota; check the runner's cgroup CPU limit and its parent limits. A larger affinity count alone does not increase workers beyond a detected quota. If the job times out despite the expected count, reduce the selected test set and inspect execution time separately from runner queue time.

[Delivered pull request #3247](https://github.com/the-cloud-clockwork/agentihooks/pull/3247) records a [red focused proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38552080779) with the old `-n auto` invocation and a [green focused proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38552154167) reporting four workers and 32 passing tests. The regression test `test_a_focused_proof_sizes_pytest_workers_from_the_cpu_quota` pins the selected tests invocation to the shared helper. These runs establish the cap and regression coverage; they do not establish a throughput improvement for every selected test set.

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

For example, each of those three jobs currently contains this environment setting; preserve its other environment entries when changing the release:

```yaml
env:
  CODEX_RELEASE: rust-v0.160.0
```

The release must provide the Linux x86_64 musl archive named above. After promotion puts the updated seed on `main`, populate its cache without waiting for the schedule:

```bash
gh workflow run codex-cache.yml \
  --ref main
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

### Verifying the installation speedup

A passing seed proves that the binary runs; verify the consuming shard too. In a completed merge queue Tests run, Python 3.12 shard 1 must run `Restore the Codex CLI` and `Use the Codex CLI` successfully while `Install the Codex CLI` is skipped. If all three steps are skipped, the run may have reused earlier test results and does not prove a cache hit. Inspect the restore log to confirm the matched key and cache version.

Read the shard's step outcomes, substituting a completed Tests run id:

```bash
gh api --paginate \
  'repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs?per_page=100' \
  --jq '.jobs[] | select(.name == "unit (3.12, 1)") | {html_url, conclusion, steps: [.steps[] | select(.name | contains("Codex CLI"))]}'
```

Measure execution time from each job's `started_at` to `completed_at`, excluding time waiting for a runner. Compare shards within the same run. List the completed successful shards from slowest to fastest:

```bash
gh api --paginate \
  'repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs?per_page=100' \
  --jq '[.jobs[] | select(.name | startswith("unit (3.12, ")) | select(.conclusion == "success") | {name, seconds: ((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601))}] | sort_by(.seconds) | reverse'
```

The following observations use the Actions jobs API for merge queue runs before and after [the cache change](https://github.com/the-cloud-clockwork/agentihooks/pull/3504):

| Observation | Before: [38802694821](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38802694821) | After: [39048986842](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39048986842) |
|---|---|---|
| Python 3.12 shard 1 execution | 478 s | 271 s |
| Codex installation | 220 s | skipped; restore took 7 s |
| Slowest other Python 3.12 shard | shard 7, 320 s | shard 11, 309 s |
| Shard 1 lead over the slowest other shard | 158 s | none; shard 1 finished 38 s faster |

Shard 1 no longer sets the run's maximum. The two slowest Python 3.12 shards in the after run took 309 s and 307 s, a 2 s gap. This observation meets the requirement that the Codex shard not exceed the next slowest shard by more than 30 s; it does not guarantee every future run's balance. Other workflow changes also landed between these runs, so the total job reduction cannot all be attributed to this cache. The skipped install directly verifies that this run avoided the download.

For a later regression, retain both run links, the shard timings and the Codex step outcomes. A miss returns to the download fallback described above; seed the matching release on `main` before measuring another consuming run.
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

## Worker hive pod image build

The Worker hive pod workflow (`hive-worker.yml`) runs `deploy/worker/kind-proof.sh` in its `kind` job. The script builds the image `local/agentihooks-hive-worker:dev` while a kind cluster starts, loads it into the cluster and proves that a worker Pod registers with the hive and acknowledges an order. The image has two parts:

| Part | Dockerfile | Contents |
|---|---|---|
| Worker base | `docker/swarm-node/Dockerfile` | the tools: Claude CLI, Codex CLI, herdr, git and the rest of the swarm worker toolchain |
| Hive layer | `deploy/worker/Dockerfile` | a wheel of the checked out agentihooks source, installed over the base, the `wt.sh` link and the worker start script |

The hive layer's `WORKER_IMAGE` build argument defaults to the published base, `ghcr.io/the-cloud-clockwork/agentihooks-worker:dev`, which the Swarm worker image workflow (`swarm-node-image.yml`) publishes on pushes to `dev`. The proof reuses that base and builds only the hive layer, unless the base's inputs changed since it was published. Before this change every run rebuilt the base and reinstalled every tool, and the build took 133 to 137 seconds; on the published base it takes 58 to 76 seconds (see [Measurements](#measurements)).

The hive layer builds its wheel from the GitHub ref named by `AGENTIHOOKS_REF`. For the troubleshooting fast lane, `--build-arg AGENTIHOOKS_SOURCE=local` builds it from `pyproject.toml`, `README.md`, `hooks/`, `scripts/` and `profiles/` in the build context instead, so an uncommitted or unpushed fix reaches the image; the default, `git`, is unchanged.

### How the base is chosen

Before building, the script runs `deploy/worker/base-current.sh ghcr.io/the-cloud-clockwork/agentihooks-worker:dev`, which:

1. pulls the published base for `linux/amd64` and reads `source_revision`, the commit the base was built from, out of `/opt/swarm-node/manifest.json` in the image;
2. fetches that commit from `origin` at depth one;
3. compares it with `HEAD` over the base inputs: `docker/swarm-node`, `scripts/swarm_v2/worker_image.py`, `profiles` and `docs/swarm-v2/schemas`.

| Exit | Meaning | Output | Build |
|---|---|---|---|
| 0 | no base input differs from the published base's revision | none | hive layer only, on the published base the check already pulled |
| 1 | at least one base input changed | `the worker base inputs changed since <revision>` in the step log | base as `local/worker-base:dev`, then the hive layer with `--build-arg WORKER_IMAGE=local/worker-base:dev` |
| 2 | the comparison could not be made | a `Worker hive base` warning annotation naming the reason, ending `; rebuilding the worker base` | the same rebuild as exit 1 |

The three warning reasons are `the published worker base revision cannot be read` (the pull or the manifest read failed), `the published worker base revision <revision> cannot be fetched` and `the worker base inputs cannot be compared with revision <revision>`. Every failure to decide falls back to the rebuild, so a missing or unreadable base slows the run but does not fail it.

On a `dev` push that changes a base input, the Worker hive pod run and the same push's Swarm worker image run start independently. When the check pulls the base before that run publishes it, the base is rebuilt; when it pulls after, the base is current and only the hive layer is built.

The base inputs are the sources the base Dockerfile copies that the hive layer does not replace: the hive layer's wheel reinstalls `pyproject.toml`, `README.md`, `hooks/` and `scripts/`, so changes there need no base rebuild, except `scripts/swarm_v2/worker_image.py`, which the base build runs to write its manifest. `tests/deployment/test_worker_hive_image.py` reads the `COPY` sources of `docker/swarm-node/Dockerfile` and fails when one of them is neither replaced by the wheel nor covered by the input list.

### Changing the base

Change these together in one pull request; `tests/deployment/test_worker_hive_image.py` pins each:

| Setting | Where |
|---|---|
| Published base image | the `base-current.sh` argument in `kind-proof.sh`, the `ARG WORKER_IMAGE=` default in `deploy/worker/Dockerfile` and the test's `BASE` |
| Base inputs | the `git diff` paths in `base-current.sh` and the test's `INPUTS`; add a path when the base Dockerfile starts copying a new source |

Limits: the check pulls and compares only the `linux/amd64` base, the kind node is pinned to `mirror.gcr.io/kindest/node:v1.37.0`, and the script needs `docker`, `git`, `python3`, `kind`, `kubectl`, `helm` and `openssl` on the runner and writes its output under `$HOME/scratchpad/worker-hive-proof`.

The rest of the proof is unchanged: the same smoke check inside the image (`claude`, `codex`, `herdr` and `git --version`, `wt.sh root` and `agentihooks hive --help`), Pod registration and order acknowledgement, and on a `dev` push the `kind` job, which starts only after the `contracts` job passes, publishes the image it proved straight from its own Docker daemon.

### Reading the result

The build step prints, and saves to `build.txt` in the `worker-hive-proof` artifact:

```text
worker_hive_image_build_seconds=58
worker_base=published
```

`worker_base` is `published` when only the hive layer was built and `rebuilt` when the base was built too. `worker_hive_image_build_seconds` covers the base check and both builds.

Read both lines from a run's log, substituting the run id:

```bash
gh run view <run id> --log \
  | grep -E 'worker_hive_image_build_seconds=|worker_base=|Worker hive base|worker base inputs changed'
```

Or download the artifact:

```bash
gh run download <run id> \
  --name worker-hive-proof \
  --dir worker-hive-proof
```

Read the revision the published base was built from:

```bash
docker run --rm --entrypoint python \
  ghcr.io/the-cloud-clockwork/agentihooks-worker:dev \
  -c 'import json; print(json.load(open("/opt/swarm-node/manifest.json"))["source_revision"])'
```

Run the full proof on a branch without waiting for a `dev` push:

```bash
gh workflow run hive-worker.yml \
  --ref <branch>
```

Run only the chart proof; `contracts_only=true` skips the `kind` job:

```bash
gh workflow run hive-worker.yml \
  --ref <branch> \
  -f contracts_only=true
```

### Measurements

Build seconds from the step log; `kind` job seconds from the Actions jobs API, `started_at` to `completed_at`.

| Run | When | `worker_base` | Build | `kind` job |
|---|---|---|---|---|
| [39100438484](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39100438484) | before [#4400](https://github.com/the-cloud-clockwork/agentihooks/pull/4400) | base always built | 133 s | 259 s |
| [39213689135](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39213689135) | after | published | 60 s | not measured |
| [39214709671](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39214709671) | after | published | 76 s | not measured |
| [39215504493](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39215504493) | after | published | 58 s | 158 s |
| [39210694361](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39210694361) | after, base inputs changed since `d7b3ce163` | rebuilt | 226 s | 340 s |

Runs [39100700634](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39100700634) and [39099280946](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39099280946) before the change printed 134 and 137 seconds. A run that reuses the published base still pulls it, so its build time depends on the runner's image cache and registry speed.

### Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `worker_base=rebuilt` with `the worker base inputs changed since <revision>` | a base input changed since the published base was built, or this push's Swarm worker image run has not published yet | none; the run proves the Pod on the base it built. Check that the Swarm worker image run on `dev` published |
| `worker_base=rebuilt` with a `Worker hive base` warning | the base could not be pulled or read, or its revision could not be fetched or compared | read the warning's reason; pull the base by hand or check that its revision still exists on `origin` |
| `worker_base=published` but the Pod lacks a base change | a path the base copies is missing from the base inputs | add the path to `base-current.sh` and the test's `INPUTS`; `test_base_inputs_cover_every_base_source_the_wheel_does_not_replace` should fail first |
| `pod did not register a live hive`, `pod did not acquire hive membership` or `scratch order not acknowledged` | the worker Pod started but failed the hive proof; the build choice does not cause this | read `pods.txt`, `failed-worker.txt`, `previous-worker.txt`, `join.txt` and `previous-join.txt` in the `worker-hive-proof` artifact, written when the script fails |
| `test_worker_hive_image.py` fails on the build lines | `kind-proof.sh` no longer has one hive-only build and a base plus hive rebuild branch | restore that shape; the tests pin both branches and the `WORKER_IMAGE` default |

## Historical measurements

For Helm chart proof timing, see [Helm kind proof input keys](/docs/reference/ci-result-cache/#helm-kind-proof-input-keys): the declared inputs, skipped job versus skipped steps, branch hit measurements and outstanding dev comparison.

For branch mutation preflight, see [Measured shards and split mutants](reference/mutation-gate.md#measured-shards-and-split-mutants): heavy mutants run a shared fast test head plus separate tail slices on distinct shards, and the merge job combines their results into one grade per mutant. The reference includes planning defaults, proof commands, completeness checks and the distinction between estimated work and observed mutation step timing.

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

## Python 3.12 unit stage in twenty shards (20 October 2026)

Python 3.12 is the only unit leg, so its twelve coverage shards set the unit stage. Over the last ten successful full scope pull request Tests runs the slowest 3.12 shard took a median 388 seconds and the unit stage, from its first job's creation to its last job's end, a median 526 seconds against its 300 second budget; push to Gate Required took a median 984 seconds. The same suite now splits twenty ways on the same measured durations, fewer jobs than the 24 unit jobs (twelve per version) those runs held before Python 3.11 left the matrix, so each shard runs about 180 seconds of tests plus its fixed setup. Measured with the task's script over the runs before and after ([issue 5339](https://github.com/the-cloud-clockwork/agentihooks/issues/5339)).
