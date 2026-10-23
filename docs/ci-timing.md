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

## Doctor CI speed reads across busy windows

The [Doctor timer](pillars/swarm.md#doctor-timer-pass) reads Actions runs and
jobs every tick. An incomplete read preserves its window and the completed
runs already read, so a window with more runs than the per pass API budget
can finish over later passes. Runner wait and tier time findings therefore
retain the jobs collected earlier instead of repeatedly spending the entire
budget on the same finished runs.

### Completed jobs and retry behavior

Discovery still reads workflow metadata. Saved jobs are reused only when both
the saved run and the newly discovered run are `completed`, with the same
`run_attempt`. New runs, active runs and reruns fetch jobs again with
`filter=latest`. Reuse does not freeze the status of an active workflow or
carry an earlier attempt's jobs into a rerun.

After an incomplete read, the timer merges every successfully read completed
run into its `known` state in Redis, including successes from the same batch
as a failed jobs request. It retains only job identifiers, names, status,
labels and creation, start and completion timestamps. Job steps and other
unused response fields are not saved. Another failed pass preserves that
progress. The timer advances its window only after a successful snapshot,
then clears the saved completed jobs for that snapshot and keeps unfinished
run identifiers for later reads. This is retry state, not a permanent cache
of every Actions run.

For example, the delivered regression fixture discovers 100 completed runs.
Its first pass reads 54 before exhausting the budget. An intervening API
outage leaves those 54 saved. The next successful pass fetches the remaining
46 jobs responses and reports all 100 runs and jobs with 52 API calls. The
fixture's counts depend on its discovery pages; they are not a fixed jobs
allowance for a live pass.

### Budgets and configuration

No new operator flag or environment variable enables reuse. It is automatic
in the Doctor timer and uses the existing Redis state. The reader's current
code defaults are:

| Reader input | Default | Meaning |
|---|---|---|
| `budget` | `(60, 25)` | At most 60 API calls and a 25 second deadline per read |
| Jobs pool | Six workers | Concurrent requests share the call and time budget |
| `known` | `None` | Completed run records supplied by the timer on retries |
| `pending` | `None` | Previously unfinished runs to look up if discovery missed them |

For a reviewed reader integration, `read(since_ms, now_ms,
known=previous_completed, pending=unfinished_ids, budget=(60, 25))` preserves
these defaults explicitly. `known` is keyed by string run id, for example:

```json
{
  "123456": {
    "status": "completed",
    "run_attempt": 1,
    "jobs": [{
      "id": 987654,
      "name": "lint",
      "status": "completed",
      "labels": ["ubuntu-latest"],
      "created_at": "2026-10-18T12:00:00Z",
      "started_at": "2026-10-18T12:00:01Z",
      "completed_at": "2026-10-18T12:00:10Z"
    }]
  }
}
```

This illustrates the internal input; operators should let the timer manage
it. Changing the default budget or pool requires a code change through CI.
Discovery, pagination, pending lookups and fresh jobs requests all consume
calls. A cold window can still be incomplete, and continuous arrivals or
active jobs can consume the budget faster than retry progress accumulates.
Reuse makes a bounded backlog converge when enough capacity remains; it does
not guarantee completion for unbounded traffic or an unavailable API.

### Long gaps read in bounded slices

The timer keeps the end of its last complete read as `last` in the Doctor's
`ci-speed` Redis record. Each tick reads one slice, from `last` to whichever
comes first: ten minutes after `last` (`SPEED_SLICE_MINUTES`) or the present.
A Doctor with no saved `last` starts one minute before the present. A long
gap, such as a tick outage overnight, is therefore never read as one window
that grows with every pass: each pass discovers at most the runs created in
one ten minute slice, plus pending runs and, once the slice reaches the
present, the runs listed by active status.

| Slice read | `last` afterwards | `pending` afterwards | Saved completed jobs | Action line status |
|---|---|---|---|---|
| Complete, slice ends before the present | Slice end | Unfinished runs of this slice | Cleared | `catching up` |
| Complete, slice reaches the present | The present | Unfinished runs of this slice | Cleared | `complete` |
| Incomplete, any slice | Unchanged | Unchanged | Merged with this pass's completed runs | `incomplete: Actions API read failed: ...` |

A slice that ends before the present lists only its `created` window. The
active status lists and their settle reads run only on the slice that reaches
the present, because an earlier slice cannot see runs that are active now.
Unfinished runs of a complete slice become `pending` and are looked up by id
on the next pass.

Every pass moves the slice ten minutes while the present moves one tick, so a
gap of N minutes reaches the present after about N / 9 complete passes at the
minute cadence. The delivered regression starts 200 minutes behind with five
runs a minute and 42 active runs: the first pass reports `catching up`, every
pass discovers at most one slice's 51 runs plus the 42 active runs, and the
test requires `complete` on every read from a start tick below 35 through the
last tick, a looser bound than the 22 passes the estimate gives.

A slice whose own runs exceed the call budget still finishes when the budget
runs out during the jobs requests: the completed jobs already read are saved, as
described above, and the next pass reuses them. When the budget runs out while
the slice's run lists or pending lookups are still being read, no jobs were
read and nothing is saved, so the next pass repeats the same slice from the
start.

API failures keep their existing text and behavior: the read reports
`incomplete`, raises the `CI speed API read is incomplete` finding unless a
[GitHub incident holds findings](#github-incident-holds-and-grouped-findings), and retries the same slice. A `catching up`
line is a successful read and raises no read failure finding. The record
reports `complete` as true only on a complete read that reaches the present.

Runner wait and tier time crossings are judged on every slice, including
`catching up` slices. The timer groups them into the detection window that
contains the present, not the window in which the runs were created, so a
catch up after an outage can notify both masters of findings about runs
created hours earlier. Their evidence names the runs, so compare each run's
creation time with the outage before acting on such a finding.

Once a master gives a runner wait or tier time finding a verdict, new runs in
the same pool or tier stay silent while the worst finished run is at or under
the judged worst plus 20 percent (`GROWTH_MARGIN` in the CI speed detector).
A worse run past that margin, or the end of the verdict cooldown, sends the
finding once per verdict; later runs stay silent until a new verdict.

The ten minute slice is a module constant in the Doctor loop. No flag or
environment variable changes it; a change goes through a pull request and CI.

### Active run lists whose count lags

Besides the `created` window list, a read that reaches the present also lists
runs by each active status: `queued`, `in_progress`, `waiting`, `pending` and
`requested`. GitHub's status filtered `total_count` can lag the list rows by a
few seconds. It still counts a run that already left the status, for example a
Tests run that moved from `queued` to `completed`, while the rows no longer
return it. Read only probes on 19 October 2026 showed the count matching the
rows again within one to five seconds of re-reading.

The reader therefore re-reads a short active status list before it reports
incomplete:

1. The first read returns fewer rows than `total_count` on a single page.
2. The reader retries at once from page 1.
3. If the list is still short, it makes up to `SETTLE_READS` (4) more reads,
   pausing one second before each.
4. The read completes as soon as one read returns as many rows as its count.
   A list still short after the last read raises the pagination error with
   the endpoint, page, rows and total of that last read.

A single short status list therefore costs at most six API calls and four
seconds of waiting, both counted against the read's call and time budget.
Settle reads apply only where the count is known to lag:

| List | Retries when short |
|---|---|
| Active status list, single page | One immediate retry, then up to four reads one second apart |
| Active status list, more than one page | One immediate retry, no settle reads |
| `created` window list | One immediate retry, no settle reads |
| Jobs of a run (`filter=latest`) and one run attempt's jobs | One immediate retry, no settle reads |

A settle read is skipped when two seconds or less of the read's deadline
remain; the read then reports incomplete instead of exhausting its budget on
pauses. Historical slices, the windows the timer reads while `catching up`,
and Doctor measure windows do not list active statuses, so they make no settle
reads.

An earlier rule from
[pull request 4448](https://github.com/the-cloud-clockwork/agentihooks/pull/4448)
still applies inside every read: when `total_count` changes between pages of
one read, the list is accepted once its last page is short, and rows are
deduplicated by id.

`SETTLE_READS`, the one second pause and the two second margin are module
constants in the Doctor CI speed reader. No flag or environment variable
changes them; a change goes through a pull request and CI. A lagging count
that settles leaves no trace in the action line: the read reports
`complete` with the same counts as any other complete read.

### Which runs count toward tier wall time

The `ci tier wall time` finding measures how long a workflow run took on the
pull request tier (`pull_request` event) or the queue tier (`merge_group`
event). Runs of other events are not measured. A pull request run is measured
only when it has a `Gate — Required` job or runs
`.github/workflows/test.yml`.

A run is judged once, after GitHub reports its status as `completed`, and
only when its conclusion is `success` or `failure`:

| Run status and conclusion | Tier wall time finding |
|---|---|
| `completed`, `success` | Judged |
| `completed`, `failure` | Judged |
| `completed`, `cancelled`, including a run cancelled because a newer push superseded it | Skipped |
| `completed`, `skipped`, `neutral`, `timed_out`, `startup_failure`, `action_required`, `stale` or no conclusion | Skipped |
| `queued`, `in_progress` or any other unfinished status | Not judged yet; the run stays pending and is judged when a later pass reads it completed |

A cancelled run's elapsed time measures how long it ran before the newer push
replaced it, not how long the tier takes, so it raises no finding. The
`ci runner wait` finding follows the same rule: started jobs of a completed
run count toward it only when the run's conclusion is `success` or
`failure`. The `ci queue age unconfirmed` finding, for jobs with no confirmed
start, does not use the run conclusion.

Wall time runs from the earliest job creation in the run to the latest
`Gate — Required` completion. A completed run with no completed gate job ends
at its latest job completion instead. A finding needs more than 15 minutes
(`WALL_MS`, 900000 milliseconds); exactly 15 minutes raises none.

Each judged run over 15 minutes adds one evidence line naming its conclusion:

```text
run 39788224362, tier pull_request, conclusion success, wall time 1223 seconds
```

The timer groups the runs of each tier into one finding per detection window,
`ci-tier-wall-time/pull_request` or `ci-tier-wall-time/merge_group`. A notice
to the masters reads, for example:

```text
- ci-tier-wall-time/pull_request: pull request tier took up to 20.38 minutes in 1 run. Evidence: 1 run, pull request, worst 1223 seconds, median 1223 seconds, runs 39788224362; run 39788224362, tier pull_request, conclusion success, wall time 1223 seconds
```

The counted conclusions (`COUNTED`) and the threshold are module constants in
the Doctor CI speed detector. No flag or environment variable changes them; a
change goes through a pull request and CI.

To check a run named in a finding, read its status and conclusion:

```bash
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run id> \
  --jq '{event, status, conclusion, created_at, updated_at}'
```

| Symptom | Interpretation and action |
|---|---|
| A finding's evidence names a run whose conclusion is now `cancelled` | Read the conclusion the evidence states. A notice written before the detector judged only completed runs could name a run as `in_progress` or `queued` that was cancelled later; a current notice names only `success` or `failure`. |
| A superseded pull request run raises no finding though it ran over 15 minutes | Expected: its conclusion is `cancelled`. Measure the run that replaced it. |
| A run over 15 minutes raises no finding yet | Check its status. An unfinished run is judged when a later pass reads it completed. |
| A pull request run over 15 minutes never raises a finding | Check that it has a `Gate — Required` job or runs the `.github/workflows/test.yml` workflow. |

### Measure a CI run and its class over a window

`agentihooks doctor <slug> measure FINDING` reads CI speed data directly for
`ci-tier-wall-time`, `ci-runner-wait` and `ci-queue-age-unconfirmed` targets.
Use the watched swarm or its linked Doctor ledger as `<slug>`. The command
requires that Doctor pairing and access to the repository's Actions API
through the existing authenticated `gh` installation.

A run target prints that attempt's own value in whole seconds, followed by
its class summary. It reads the named attempt even when that run falls
outside the summary window:

```bash
agentihooks doctor rig-grade-swarm measure \
  ci-tier-wall-time/39356970006-1
agentihooks doctor rig-grade-swarm measure \
  'ci-runner-wait/39280699907-1-["anton-oss-light"]'
```

The delivery proof recorded first lines of
`ci-tier-wall-time/39356970006-1 952` and
`ci-runner-wait/39280699907-1-["anton-oss-light"] 26`.
These are historical readings, not expected values for current traffic. A
runner wait target reports the longest confirmed wait among jobs on that
exact label set. Without labels, a run target reports its longest wait and
prints summaries for every label set in the window. Quote JSON label sets
so the shell preserves the double quotes. For a run target, copy the sorted
label set from the finding.

Use a grouped target to measure one tier or label set, or a bare class name
to print every scope present in the window:

```bash
agentihooks doctor rig-grade-swarm measure ci-tier-wall-time/pull_request
agentihooks doctor rig-grade-swarm measure ci-tier-wall-time/merge_group
agentihooks doctor rig-grade-swarm measure \
  'ci-runner-wait/["anton-oss-light"]'
agentihooks doctor rig-grade-swarm measure ci-tier-wall-time
agentihooks doctor rig-grade-swarm measure ci-runner-wait
agentihooks doctor rig-grade-swarm measure ci-queue-age-unconfirmed
```

Tier names in targets are `pull_request` and `merge_group`; output names them
`pull request` and `queue`. Grouped runner targets sort and deduplicate labels
and require an exact label set, not a match on one label within a larger set.

#### Window and summary rules

The default summary covers the hour ending when the command starts. Set both
`--since` and `--until` to compare two equal windows around a fix's application:

```bash
agentihooks doctor rig-grade-swarm measure ci-tier-wall-time/merge_group \
  --since 2026-10-20T08:00:00Z --until 2026-10-20T09:00:00Z
agentihooks doctor rig-grade-swarm measure ci-tier-wall-time/merge_group \
  --since 2026-10-20T11:00:00Z --until 2026-10-20T12:00:00Z
```

These illustrate equal one hour bounds; choose bounds around the actual
application and retain both commands and outputs with the task proof. Times
must parse as ISO timestamps, the start must precede the end and the end
must not be in the future. Use an explicit UTC zone as above.

A class line has this form; the numbers here illustrate the format:

```text
ci-tier-wall-time queue, window 2026-10-20T08:00:00Z to 2026-10-20T09:00:00Z: 2 of 3 runs over 900 seconds, median 952 seconds, worst 1100 seconds
```

The window selects runs by creation time through the CI speed reader. The
measure read uses `active=False`: it does not add older active runs through
the timer's active status discovery. It reads the discovered runs' current
state, so an unfinished run can change when the same window is read later.
This is a window count, not a count of stored findings or an automatically
normalized per hour rate.

| Class | One sample per run and scope | Over threshold |
|---|---|---|
| `ci-tier-wall-time` | Wall time for the run's eligible tier | Strictly more than 900 seconds |
| `ci-runner-wait` | Longest creation to confirmed start wait for each exact label set | Strictly more than 300 seconds |
| `ci-queue-age-unconfirmed` | Longest age for jobs without a confirmed start on each exact label set; ends at job completion if present, otherwise the command's read time | Strictly more than 900 seconds |

All measurable samples contribute to the denominator, median and worst,
including values below threshold. Counts compare milliseconds before display
rounding. Seconds are truncated; for an even sample count the median is the
lower middle sample. Multiple jobs on one label set in one run contribute
one longest wait, not one sample per job. Thus a run can contribute once to
each of several label set summaries.

Wall time uses the eligibility and endpoints described in
[Which runs count toward tier wall time](#which-runs-count-toward-tier-wall-time).
Explicit measure also permits provisional wall time for unfinished runs,
ending at the current time if there is no completed gate. The detector waits
until completion before judging a wall time finding. Wait measurement reads
job timing directly; it does not apply the detector's completed run conclusion
filter to confirmed waits.

A finished run's recorded duration cannot improve after a fix. Its value
checks the original evidence. Compare the same class and scope over equal
before and after windows to assess the fix, and retain sample counts so a
quiet window is distinguishable from faster CI.

#### Configuration, refusals and delivery proof

No new environment variable enables measurement. Bounds are command options;
thresholds come from the CI speed detector constants. The measure's window
read budget is `WINDOW_BUDGET = (2000, 600)`: at most 2000 API calls and a
600 second deadline. This differs from the timer's per pass budget above;
changing either requires a reviewed code change.

| Result | Meaning and action |
|---|---|
| `has no tier wall time` | The attempt has no eligible wall time, for example a completed cancelled run or missing timing data. Check the run's event, conclusion and job endpoints. |
| `unreadable` | The attempt lookup failed. Check Actions API access and availability; do not record zero. |
| `has no job on runner labels` or `match no job in the window` | The requested set has no matching measurable jobs for the run, or no matching jobs in the window. Use the reported known label sets and check the bounds. |
| Window read error or incomplete snapshot | The command refuses the class result. A run value may already have printed; it does not establish a complete class read. |
| `0 of 0 runs over ...` | No measurable samples for the selected tier or label set, or no scopes for a bare class. A label set can have jobs but no confirmed waits for `ci-runner-wait`, or no unconfirmed jobs for `ci-queue-age-unconfirmed`. Median and worst are omitted. This is no exposure, not evidence of improvement. |

[Pull request 5076](https://github.com/the-cloud-clockwork/agentihooks/pull/5076)
delivered this measurement path. Its source proof reported 818 of 818 recorded
CI speed findings printing zero under the old generic detector collection.
The [red CI run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39365602865)
failed on that behavior; the
[green CI run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39367721363)
passed the fix and mutation checks. The two historical run readings above
verify the new path; they do not claim a reduction in CI duration. An
unreadable or unmeasurable run is refused instead of becoming a false zero.

### Confirmed runner waits and unconfirmed queue age

The Doctor checks old queued job snapshots before calling them runner waits.
A job has a confirmed start when it has `started_at` and either its status is
`in_progress` or `completed`, or it has a nonempty `runner_name`. A queued
status with both a runner name and a start timestamp therefore counts as
started. A timestamp alone on a queued job does not confirm pickup.

For active jobs without a confirmed start, the reader requests the single
job endpoint once their age exceeds five minutes. A successful response
replaces the snapshot row before findings are calculated. Up to 20 jobs are
reread per pass, sharing the reader's API and time budget. Failed requests
and candidates beyond that cap contribute to `queued jobs not reread` in
the timer status line. That count describes missing rereads, not runner
shortage; a successful reread can still leave a job unconfirmed.

| Evidence | Result |
|---|---|
| Confirmed start within five minutes of creation | No runner wait |
| Confirmed start more than five minutes after creation | Eligible for `ci runner wait`, once the run completes with `success` or `failure` |
| No confirmed start, age at most 15 minutes | No queue age finding |
| No confirmed start, age more than 15 minutes | Eligible for `ci queue age unconfirmed`; elapsed time ends at the read time |

Exactly five minutes is below the runner wait trigger; exactly 15 minutes is
below the unconfirmed age trigger. Unconfirmed age does not establish a
runner supply problem. When the same run attempt and label set already has
a confirmed long wait, that wait takes precedence over its unconfirmed age.
The timer aggregates findings across runs; evidence names the runs, label
sets, affected jobs, longest wait and median wait.

#### One finding per wait

A run attempt and label set yields at most one CI speed finding for a slow
pickup. When any of its jobs has a confirmed start more than five minutes
after creation, the detector raises no `ci queue age unconfirmed` finding for
that run attempt and label set, however old its still queued siblings are.
The confirmed wait alone stands for the group, so the masters judge one
finding for one wait.

The suppression holds whenever the confirmed wait is present in the read,
including while the run is still in progress and the `ci runner wait`
finding itself is not yet judged. Such a group raises nothing until the run
completes with `success` or `failure`, then raises the runner wait. A
confirmed wait on one label set does not suppress queue age on another label
set or another run attempt.

The 15 minute unconfirmed threshold (`UNCONFIRMED_WAIT_MS`) is the five
minute runner wait trigger (`SHORT_WAIT_MS`) plus a fixed ten minute
allowance for one default Doctor pass.
A queued job that the next pass reads as started therefore raises a runner
wait, or nothing, instead of a queue age finding that the Doctor retracts
minutes later.

| Jobs in one run attempt and label set at the read | Findings |
|---|---|
| One job confirmed after 304 seconds, one still queued at 936 seconds, run completed `success` | `ci runner wait` only |
| The same jobs, run still `in_progress` | None yet |
| One job still queued at 900 seconds or less, nothing confirmed | None |
| One job still queued at 901 seconds, nothing confirmed | `ci queue age unconfirmed`, measure 901 |

The first row comes from the regression fixture: run `39278021878`, label
set `anton-oss-light`, read at `15:37:32Z`. The late job was created at
`15:31:56Z` and started at `15:37:00Z`; the sibling was created at
`15:21:56Z` and is still queued.

A queue age finding raised before a sibling confirmed is retracted on the
pass that reads the confirmation. When any job of the run attempt and label
set has a confirmed start over five minutes, the timer removes that run
attempt from the `ci-queue-age-unconfirmed/<labels>` group and keeps the
runner wait. The group finding is retracted, with the notice described
below, once no run is left in it.

#### Inspect a named job

From the watched repository, replace the example numbers with the run and
job identifiers in the finding:

```bash
run_id=39278021878
job_id=123456789
repo=the-cloud-clockwork/agentihooks
gh api "repos/$repo/actions/runs/$run_id/jobs?filter=latest" \
  --jq '.jobs[] | {id, name, status, runner_name, labels, created_at, started_at}'
gh api "repos/$repo/actions/jobs/$job_id" \
  --jq '{id, status, runner_name, created_at, started_at, completed_at}'
```

For example, a queued snapshot created at `12:00:00Z` reread at `12:06:00Z`
may return `started_at=12:00:06Z` and a runner name. Its pickup took six
seconds, so it raises no runner wait. If the reread still has no confirmed
start, it remains unconfirmed; after `12:15:00Z` its queue age can qualify
without asserting a runner wait.

A workflow concurrency hold can leave a run `pending` with an empty jobs
response. The recorded boundary in [pull request 4835](https://github.com/the-cloud-clockwork/agentihooks/pull/4835)
was run `39287956231`, held by `promotion-dev`, whose jobs API returned
`{"jobs":[],"total_count":0}`. With no job, the detector raises no runner
wait. It does not label workflow or job concurrency holds or diagnose their
cause; inspect the workflow's concurrency configuration and Actions state.
Runner lane limits are unchanged by this confirmation behavior.

#### Retractions, configuration and troubleshooting

A corrected read can supersede an earlier finding. All jobs in a run attempt
and label set confirmed within five minutes clear its runner wait and
unconfirmed age entries. A confirmed long wait supersedes its unconfirmed
age entry while preserving the real wait. The timer prunes corrected members
from its aggregates and retracts stored findings that no longer apply. It
removes their finding detail, adds `retracted_at` to an existing verdict and
sends an FYI notice to both the watched master and the Doctor master:

```text
Retracted Doctor findings on the swarm rig-grade-swarm: a corrected read of their jobs supersedes them.
```

The status line reports the number actually retracted. Historical notices
remain historical evidence; use the corrected finding and retraction notice
when judging the current state.

Confirmation is automatic. There is no enable switch or environment override.
`SHORT_WAIT_MS` is 300000, `UNCONFIRMED_WAIT_MS` is 900000 and `REREADS` is 20;
these are code constants changed through CI. The existing
[reader budgets](#budgets-and-configuration) apply to rereads too.

| Symptom | Check and action |
|---|---|
| A queued snapshot suggests a long wait | Read the single job endpoint and compare creation, start and runner assignment before attributing it to runners. |
| `queued jobs not reread` is nonzero | Check the read error and API budget; later passes can reread candidates. Do not interpret the count as confirmed waits. |
| Queue age appears without a runner wait | Expected when pickup is unconfirmed; inspect current Actions state and concurrency configuration. |
| A corrected fast start still has an older notice | Check the later retraction notice and current finding; a grouped finding can remain while other members still qualify. |

#### Delivery evidence

[Pull request 4835](https://github.com/the-cloud-clockwork/agentihooks/pull/4835)
merged on 19 October 2026 and was applied at 20:40:31 UTC. Its recorded stale
snapshot regression failed in [red Proofs run 39289504095](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39289504095):
a job started in seconds had raised a 336 second runner wait. The fix passed
[Tests run 39292565892](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39292565892)
and [Mutation preflight run 39292554608](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39292554608).

The [source task's measurement](https://github.com/the-cloud-clockwork/agentihooks/issues/4834)
compared two hour windows on 19 October. From 15:00 to 17:00 UTC, 114 of 124
runner wait findings referred to job groups whose every job started within
60 seconds by the jobs API: 57 false findings per hour. From 20:41 to 22:41 UTC,
after application, eight runner wait findings remained and zero were in that
false class: 0 per hour. The eight retained findings had confirmed waits of
306 to 696 seconds. This measures removal of false findings, not increased
runner capacity.

[Pull request 5112](https://github.com/the-cloud-clockwork/agentihooks/pull/5112),
for [issue 5109](https://github.com/the-cloud-clockwork/agentihooks/issues/5109),
merged on 20 October 2026 at 06:48 UTC and was applied at 07:37 UTC. It added
the confirmed wait precedence and raised the unconfirmed threshold from five
to 15 minutes. Before it, in the hour from 20:41 UTC on 19 October, five
unconfirmed queue age findings were raised and all five retracted within
about five minutes once their jobs started: runs 39323176234, 39323181003,
39325949794, 39328650183 and 39328654494. Four of those runs also carried a
confirmed runner wait for the same jobs. With only the new tests pushed, the
regression tests failed in
[red Proofs run 39370571622](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39370571622):
a confirmed wait with a queued sibling raised both findings, and a 336 second
unconfirmed job raised queue age. The fix passed
[Tests run 39373027273](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39373027273)
and [Mutation preflight run 39373022081](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39373022081).
The source task targeted zero retracted queue age findings per hour; no after
window count was recorded on it.

### GitHub incident holds and grouped findings

Two rules keep CI speed findings to judgments a master can act on. While GitHub
itself reports an unresolved incident affecting Actions, the CI speed pass
reads runs but raises no findings. Outside an incident, crossings of the same
class on the same label set or tier within one Doctor pass window arrive as one
finding that names every run, instead of one finding per run.

#### Which incidents hold findings

Every pass reads GitHub's status page,
`https://www.githubstatus.com/api/v2/incidents/unresolved.json`, with a ten
second timeout. An unresolved incident holds findings when one of its
components is named exactly `Actions`, or when its name contains the word
`Actions`; some Actions incidents list no component and name Actions only in
their title. An incident that affects only Pages, Webhooks or another component
holds nothing. The incident's start is its `started_at`, or its `created_at`
when GitHub gives no start.

The timer records each incident in the `incidents` field of the Doctor's
`ci-speed` Redis record. An incident is open while every successful read lists
it, and closes, with an end time of that pass, on the first successful read
that no longer lists it. Closed incidents are kept for 24 hours (`KEEP_MS`) so
later runs can still be labelled against their window. An incident that GitHub
lists again after it closed reopens under the same id and holds again.

A status page read that fails, for a network error, a timeout or a body that is
not the expected JSON, never holds findings and changes no recorded incident:
open incidents stay open and none is closed. The pass then raises findings as
if no incident were open.

#### What a hold does

While at least one open incident is listed by the current read:

| Part of the pass | During the hold |
|---|---|
| Actions run and jobs read | Unchanged: runs, jobs and API calls are read and counted, and the window advances |
| `ci runner wait`, `ci queue age unconfirmed`, `ci tier wall time` | Not raised; this pass's crossings are not added to any group |
| `ci skipped record lag` | Not raised |
| `CI speed API read is incomplete` | Not raised, even when the read fails |
| Retractions of earlier findings | Unchanged: a corrected read still retracts a superseded finding |
| Notice to the masters | One FYI notice per incident, to both masters |

The notice reaches the watched swarm's master and the Doctor master once per
incident, as an FYI notice that needs no reply. On the 19 October incident it
read:

```text
Doctor CI speed findings on the swarm rig-grade-swarm are held: GitHub reports an unresolved incident affecting Actions, Degraded availability for GitHub Actions, GitHub Pages and Webhooks, since 19 October 15:49 UTC. https://stspg.io/nrv2k8s68bwn Runs are still read and counted; findings resume when it resolves, and runs with jobs created during it are labelled.
```

An incident that reopens is not announced again. Each held pass ends its
action line with one suffix per open incident, and reports 0 crossings:

```text
doctor CI speed: complete; 20 runs read of 20 discovered, ... 0 crossings, ... queued jobs not reread; findings held during GitHub incident Degraded availability for GitHub Actions, GitHub Pages and Webhooks
```

Crossings of runs that finished during the hold are dropped, not stored for
later. A run still unfinished when its slice completes stays pending and is
read again, so a run queued during the incident and judged after it resolves
can still raise a finding.

#### After an incident resolves

The first pass whose successful read no longer lists the incident raises
findings again. A grouped finding with members whose jobs were created between
an incident's start and its end, or the present while it is open, carries an
extra evidence line naming how many runs and which incidents:

```text
5 of these runs had jobs created during the GitHub incident Degraded availability for GitHub Actions, GitHub Pages and Webhooks
```

Weigh such runs against the incident before reading them as a runner or tier
problem.

#### One finding per class and label set or tier per pass

Runner wait and queue age crossings are grouped by their runner label set, tier
wall time crossings by their tier, pull request or queue. Each group is one
finding per Doctor pass window. The window is `AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES`
wide (default 10, see
[Doctor timer pass](pillars/swarm.md#doctor-timer-pass)) and aligned to
multiples of that width in UTC. Finding ids name the class and the scope only:

| Class | Grouped by | Example id |
|---|---|---|
| `ci runner wait` | Runner label set | `ci-runner-wait/["anton-oss", "self-hosted"]` |
| `ci queue age unconfirmed` | Runner label set | `ci-queue-age-unconfirmed/["anton-oss", "self-hosted"]` |
| `ci tier wall time` | Tier | `ci-tier-wall-time/pull_request`, `ci-tier-wall-time/merge_group` |

The summary names the scope, the worst measure in minutes and the run count.
The first evidence line names the run count, the scope, the worst and median
measure in seconds and every run id, ordered by run id and attempt. One line per
run follows, after the incident line when there is one. Ten queued runs on one
label set in one pass give, for example:

```text
- ci-queue-age-unconfirmed/["anton-oss", "self-hosted"]: anton-oss, self-hosted jobs read as queued up to 8.00 minutes in 10 runs. Evidence: 10 runs, anton-oss, self-hosted, worst 480 seconds, median 330.0 seconds, runs 1, 2, 3, 4, 5, 6, 7, 8, 9, 10; ...
```

A rerun appears as `<run id> attempt <n>`. The finding's measure is the worst
member's measure. A later run that crosses in the same window joins the group
and grows its evidence without another notice. A new window starts the group
afresh under the same id, so one wait that spans a window boundary stays one
finding with one notice. Once a master gives the finding a verdict, the growth
margin described in [Long gaps read in bounded slices](#long-gaps-read-in-bounded-slices)
decides whether it is sent again. A corrected read removes a superseded run
from its group, and the group is retracted once no run is left; see
[Retractions, configuration and troubleshooting](#retractions-configuration-and-troubleshooting).

#### Configuration

The hold and the grouping are automatic. No flag or environment variable turns
either off. The status page address, the `Actions` component name and the 24
hour retention of closed incidents are module constants in the Doctor GitHub
status reader; the summary wording is a module constant in the Doctor CI speed
detector. A change goes through a pull request and CI. Only the window width
follows `AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES`, which also sets the time between
detection passes.

#### Inspect a hold

Read what the status page lists now and which incidents would hold:

```bash
curl -s https://www.githubstatus.com/api/v2/incidents/unresolved.json \
  | jq '.incidents[] | {id, name, status, started_at, created_at, components: [.components[].name]}'
```

Find the passes that held findings:

```bash
journalctl --user -t agentihooks-swarm --since '-6 hours' --no-pager -o cat \
  | grep --line-buffered 'rig-grade-swarm-doctor' \
  | grep --line-buffered 'findings held during GitHub incident'
```

| Symptom | Check and action |
|---|---|
| No CI speed findings during visibly slow CI | Look for the held suffix in the action line. If present, read the incident on the status page; findings resume on the first pass after GitHub resolves it. |
| A hold continues after GitHub shows the incident resolved | The unresolved list still names it, or the last reads failed. A failed read neither holds nor closes, so the next successful read that omits it ends the hold. |
| A slow CI hour raised findings although GitHub had an incident | The incident names neither an `Actions` component nor Actions in its title, or the status page could not be read. Both hold nothing by design. |
| A finding carries the incident evidence line | Some of its runs had jobs created inside the incident window. Compare those runs with the rest before acting. |
| Ten slow runs raised one finding | Expected: one finding per class and label set or tier per pass. Read the run ids in the first evidence line. |
| A second notice for the same group | A verdict was given and a later run passed the judged worst by more than the growth margin, or the verdict cooldown ended. |

#### Delivery evidence

[Pull request 5080](https://github.com/the-cloud-clockwork/agentihooks/pull/5080)
merged on 20 October 2026 for
[issue 5078](https://github.com/the-cloud-clockwork/agentihooks/issues/5078).
Its source was the 19 October GitHub incident that degraded Actions, Pages and
Webhooks from 15:49 UTC: the runner wait and tier wall time detectors raised
one finding per run and label, over twenty per ten minute pass, to both
masters, about one hundred twenty CI speed findings in the incident hour. The
watched master judged thirty five of them and the Doctor master about fifty.

The [red Proofs run 39366548682](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39366548682)
ran the new tests on dev code: a recorded fixture of that incident raised
twenty per run findings, and ten runs on one label raised ten findings. The
[green Proofs run 39366593106](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39366593106)
passed with no findings during the incident and one grouped finding after it,
and [Tests run 39369093533](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39369093533)
passed every check and mutation shard. No live hour under a later Actions
incident has been measured since the merge.

Later pull requests changed the grouping:
[5715](https://github.com/the-cloud-clockwork/agentihooks/pull/5715) took the
window out of the finding id, so a group keeps its id from one window to the
next, and [5707](https://github.com/the-cloud-clockwork/agentihooks/pull/5707)
sent the hold notice as an FYI notice.

### Inspect and diagnose a read

Read the existing timer actions without starting another timer:

```bash
journalctl --user -t agentihooks-swarm \
  --since '2026-10-18 12:53:00 UTC' \
  --until '2026-10-18 13:23:00 UTC' --no-pager -o cat \
  | grep --line-buffered 'rig-grade-swarm-doctor' \
  | grep --line-buffered 'doctor CI speed:'
```

Replace the Doctor swarm name and both UTC boundaries for a new measurement.
Keep the raw lines and report the number of reads as well as the number
containing the budget error. Use equal thirty minute windows and start the
after window only once the fix is applied to the running Doctor. A zero count
with no retained action lines does not demonstrate successful reads.

Actions report `runs read of ... discovered`, jobs, crossings and API calls.
Reused jobs count as read jobs even though they consumed no new jobs request.
Current actions also report retractions and queued jobs not reread. Current
window slicing can report `catching up`: that snapshot succeeded, but the
reader has not yet reached the present. It is distinct from an API failure;
see [Long gaps read in bounded slices](#long-gaps-read-in-bounded-slices).
For example, on 19 October 2026 at 11:09 UTC, after pull request 4729 merged
at 09:57 UTC and introduced the `catching up` status, the Doctor logged a
backlog slice in that day's shorter action line format, before the retraction
and reread counts were added:

```text
rig-grade-swarm-doctor: doctor CI speed: catching up; 90 runs read of 90 discovered, 1028 jobs, 3 crossings, 47 API calls
```

| Symptom | Interpretation and action |
|---|---|
| `incomplete: Actions API read failed: RuntimeError: Actions API request or time budget exhausted` | The call or time budget was reached. Compare successive run counts; completed successes remain available for retry. |
| Another `Actions API read failed: ...` error | Inspect the named request, authentication, timeout or pagination failure. Successful completed jobs remain saved; the read still reports incomplete. |
| `Actions API pagination ended before the full count: endpoint=...?status=...; page=1; rows=N; total=N+1` | A single page active list stayed one or more runs short through all six reads, or the deadline left no room for a settle read. Re-read that endpoint by hand and compare `total_count` with the rows; a count that never settles points at the Actions API, not the reader. |
| `Actions API pagination ended before the full count: ...; page=2` or higher | A multi page list stayed short after its retry. It is not settled; check whether runs arrived or left during the read and compare the next pass. |
| Rerun or active workflow fetches jobs again | Expected: reuse requires completed status and the same attempt. |
| `catching up` | A historical slice completed; later ticks continue toward the present. |
| `catching up` for more than N / 9 ticks, where N is the gap in minutes | Check the tick cadence first: slices advance one per tick, so a stalled or slow tick stretches the catch up. Then look for incomplete reads between the `catching up` lines; each one retries the same slice. |
| `runs ... discovered` keeps rising pass after pass while `catching up` | Not expected from slicing, which bounds discovery to one slice. Compare with the number of runs actually created in ten minutes of that period before suspecting the reader. |
| Backlog never clears | Compare discovery and new or active workload with remaining request capacity; do not infer completion from a large saved count. |

### Delivered proof and measurement limits

[Pull request 4547](https://github.com/the-cloud-clockwork/agentihooks/pull/4547)
merged on 18 October 2026. Its
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39138882973)
showed that a second pass over 100 completed runs remained incomplete; its
[green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39139392387)
passed with carryover. The regression checks also cover changed attempts,
active runs and preservation of successful jobs when another request fails.

[Pull request 4640](https://github.com/the-cloud-clockwork/agentihooks/pull/4640)
merged on 19 October 2026 and added the settle reads. Its
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39204844366)
failed the observed shape, a `status=queued` list of six rows with a count of
seven, on the old code; its
[green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39208181014)
passed on the merged head. The regression checks cover counts that settle
after one, two and five stale reads, a count still stale after six reads,
the deadline margin, and short multi page and job lists that are not settled.
Its source task recorded 4 pagination incomplete reads out of 17 during
18 October 2026, 12:53–13:23 UTC, after pull request 4448 applied. That is
the historical before measurement for the settle reads.

The source task recorded 13 budget exhausted reads out of 17 during
18 October 2026, 12:53–13:23 UTC. That is the historical before measurement.
The regression proves convergence for its controlled backlog; it does not
supply a comparable live thirty minute after count.

[Pull request 4729](https://github.com/the-cloud-clockwork/agentihooks/pull/4729)
merged on 19 October 2026 and added the bounded slices. Its
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39224134631)
failed the 200 minute gap regression on the old code; its
[green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39225901866)
passed on the final head. Its source task recorded the live before shape after
an overnight tick outage from `agentihooks-swarm` journal lines matching
`doctor CI speed`, 19 October 2026, 05:40–06:15 UTC: 9 of 15 reads
exhausted the budget and 6 failed pagination, while discovered runs grew
49, 96, 108, 189, 202, 208, 229, 301 and 305 and runs read rose only from 47
to 249 per pass. The journal command in
[Inspect and diagnose a read](#inspect-and-diagnose-a-read), with its
boundaries set to 22 October 2026, 15:00–15:30 UTC, against the running
Doctor after the merge, returned 10 reads, all `complete`, none with the budget error, the
largest discovering 16 runs. That window had no gap to catch up and is not an
equal-load comparison with the outage window.

[Pull request 4760](https://github.com/the-cloud-clockwork/agentihooks/pull/4760)
merged on 19 October 2026 and added the conclusion rule. Its source report was
pull request run 39166378179, which ended cancelled with 22 jobs cancelled
after a newer push, yet was raised as a 15.3 minute pull request tier time. Its
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39237322057)
failed 22 new assertions on the old code, covering each excluded conclusion,
the missing conclusion in the evidence and a 22 job superseded run fixture;
its [green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39237817270)
passed on the merged head.
[Pull request 5901](https://github.com/the-cloud-clockwork/agentihooks/pull/5901)
merged on 22 October 2026 and moved judging to completed runs only, so a run
is no longer judged while it is unfinished.

The source task counted tier wall time evidence lines in Doctor timer notices
to both masters and read each run's current conclusion. Over the 24 hours
from 18 October 2026, 11:00 UTC, before the merge, 16 of 134 findings were on
runs whose conclusion is now `cancelled`, run 39166378179 among them. Over
the 24 hours from 19 October 2026, 13:15 UTC, after the merge, 47 of 198
were, but none of the 198 evidence lines stated `cancelled`: they stated
`in_progress` 102 times, `queued` 38, `success` 52 and `failure` 6, so the
cancelled runs were judged while still running and cancelled afterwards.
Over the 12 hours from 22 October 2026, 14:30 UTC, after pull request 5901
applied, the notices held no tier wall time evidence lines. That window
carried no findings at all and is not an equal-load comparison.

## Delivery tiers

Pull request checks run once on the current head: lint, format, size, static analysis, changed-line coverage and mutation, and touched tests. The `dev` merge queue runs the full unit suite and test count checks on the merge group; it does not repeat the pull request checks. Shard time budgets do not hold the queue.

Each integration push runs the promotion tier, including module coverage, scheduled mutation reports, image and deployment smoke checks and duration refresh. A running promotion run finishes; newer heads replace only the pending promotion run. A red promotion keeps `main` unchanged while `dev` keeps merging, and the swarm promotion watch files repair work. Green promotion lets CI alone merge the graded `dev` head into `main`, tag a release and publish versioned and `:release` images. The `:dev` image remains an experimentation image.

See [Delivery tiers](/docs/reference/delivery-tiers/) for check ownership. The measurements below record earlier workflow versions; their old event assignments do not describe the current tiers. Tests and mutation runs execute in CI only.

## OCI runner migration measurements

The [Doctor measurement](https://github.com/the-cloud-clockwork/agentihooks/issues/4564#issuecomment-6252369563)
records what happened to agentihooks CI around three runner changes on
13 and 14 October 2026. Placement increased median push to `Gate Required`
time; raising runner CPU requests later reduced the merge queue median and
tail. These are historical observations of the workflow used then. Use
[Delivery tiers](reference/delivery-tiers.md) for current check ownership.

### Cohorts and measurement rules

The measurement reused the method from
[Doctor's earlier gate measurement](https://github.com/the-cloud-clockwork/agentihooks/issues/3146):
select Tests runs whose **first attempt starts** inside each named UTC window,
keep `pull_request` and `merge_group` separate, and read attempt 1 jobs.
Measure gate completion from workflow `created_at`, which is a proxy for the
push trigger rather than an observed Git push timestamp. A run can finish
after its selection window ends.

Report cohort size, completed gates, missing gates and successful gates
separately. The main p50 and p90 include completed noncancelled gates that
failed. A failed gate can finish quickly; these percentiles do not measure
time to a successful merge. Keep successful-only percentiles alongside them.
Worker image wall time is job completion minus job start; it excludes runner
pickup. Slowest unit wall time is the maximum unit job wall time per run.
Neither alone establishes the whole run's critical path.

| Comparison | Before UTC | After UTC | Window length per side |
|---|---|---|---|
| OCI placement | 13 Oct 13:10:34–19:10:34 | 13 Oct 19:10:34–14 Oct 01:10:34 | Six hours nominally; before excludes a 599 second planned cordon |
| On demand EC2 removed | 14 Oct 03:44–07:37 | 14 Oct 07:37–11:30 | 3 h 53 min |
| Runner CPU request raised | 14 Oct 07:45–11:30 | 14 Oct 11:45–15:30 | 3 h 45 min; after starts fifteen minutes after application |

All dates in this section are in 2026. The placement comparison has 21001
included seconds before and 21600 after because it excludes the planned
18:08:01–18:18 cutover. Preserve that exclusion and disclose the unequal
included durations when reusing the results.

### Placement results and cordons

[antoncore pull request 829](https://github.com/the-cloud-clockwork/antoncore/pull/829)
merged at 19:10:34 UTC on 13 October. That placement preferred OCI for `anton`
and `anton-oss`, with on demand EC2 overflow after six minutes. This describes
the intermediate migration configuration. The operator's destination was
every CI runner set on OCI, accepting slower image builds; these measurements
do not authorize moving runners back to EC2.

Times below are seconds. Each arrow reads before → after.

| Event | Cohort | Completed gates | Missing gates | Gate p50 | Gate p90 | Successful gates | Successful-only p50 / p90 |
|---|---|---|---|---|---|---|---|
| Pull request | 109 → 102 | 105 → 100 | 4 → 2 | 1076 → 2140 | 3762 → 3429 | 43 → 41 | 2574 / 4069 → 2847 / 3707 |
| Merge queue | 38 → 47 | 38 → 47 | 0 → 0 | 1637 → 3101 | 4210 → 3893 | 25 → 33 | 2272 / 4045 → 3101 / 3714 |

| Worker image event | Measured jobs | Wall p50 | Wall p90 |
|---|---|---|---|
| Pull request | 74 → 69 | 363 → 361 | 411 → 497 |
| Merge queue | 36 → 33 | 363 → 363 | 439 → 480 |

The planned 18:08 cutover is excluded from the before cohort. Two later
intervals, 19:12–19:22 and 20:05–20:15, had all EC2 CI nodes cordoned while
OCI runners were pending. **No plan was recorded for those later cordons.**
The source task records the watched master's 20:15 observation: 29 of 39
`anton-oss` runners pending for insufficient CPU on fifteen full Oracle nodes,
33 EC2 nodes cordoned, no dev merge since 17:47, and queue entries dropping
at the sixty minute check timeout.

Retain those intervals in the primary after result and report them separately:
one pull request run first started in each, with failed gates completing at
1014 and 1944 seconds respectively; no merge group run first started in
either interval. Excluding both gives 100 pull request runs, 98 completed
gates and p50 / p90 of 2175 / 3457 seconds; queue results are unchanged.
Selecting by run start does not count every already-running job affected by
the cordons.

### Hourly runner wait

The original runner wait reader reported `anton-oss` unit pickup p90 by UTC
hour, splitting wait into scale set cap, node provisioning and boot. Across
these hours it attributed wait to the runner cap, with node provisioning p90
between zero and ten seconds. Pending pods at a particular instant and this
windowed wait attribution answer different questions.

| Before hour, 13 Oct UTC | Wait p90 seconds | After hour UTC | Wait p90 seconds |
|---|---|---|---|
| 13 | 1330 | 13 Oct 19 | 1386 |
| 14 | 879 | 13 Oct 20 | 1315 |
| 15 | 368 | 13 Oct 21 | 941 |
| 16 | 345 | 13 Oct 22 | 1802 |
| 17 | 1498 | 13 Oct 23 | 1286 |
| 18 | 1951 | 14 Oct 00 | 1536 |
| 19 | 1630 | 14 Oct 01 | 1626 |

The retained whole-window p90 is 1420 → 1488 seconds, with 2219 of 2234
jobs measured before and 2126 of 2126 after. This reader was not the audited
attempt 1 gate collector: its before output also contains eighteen latest
attempt rerun jobs in hour 20 with p90 **−1297 seconds**. That is a timestamp
artifact, not negative queueing. The table omits that invalid bucket; the
quoted whole-window values remain the original report, not a recomputed,
cleaned first-attempt estimate. Do not use them as precise causal proof of
the cloud change.

### Infrastructure loss and CPU requests

Near 07:37 UTC on 14 October the operator's spot change took on demand EC2
CI nodes to zero and about ten nodes left together. Jobs on those nodes lost
runner contact. Treat that burst as infrastructure loss when interpreting
failures.

| Event around EC2 removal | Completed gates / cohort before → after | Gate p50 | Gate p90 | Slowest unit wall p50 |
|---|---|---|---|---|
| Pull request | 50 / 52 → 28 / 29 | 1256 → 1850 | 4062 → 5437 | 294 → 549 |
| Merge queue | 48 / 48 → 31 / 31 | 2071 → 2186 | 5678 → 4178 | 307 → 542 |

No Gate Required succeeded in either event's after cohort. The lower queue
p90 therefore does not establish recovery.

The watched investigation in
[antoncore issue 839](https://github.com/the-cloud-clockwork/antoncore/issues/839)
associated the unit tail regression with CPU packing: runners requested two
CPUs with a four CPU limit, allowing six to seven on a sixteen CPU OCI node
at about 96 percent CPU. Median shard execution stayed near 250 seconds
while the slowest grew from about five to about 9.5 minutes.
[antoncore pull request 841](https://github.com/the-cloud-clockwork/antoncore/pull/841)
raised the request to 3500 millicores, retaining the four CPU limit, live
about 11:30 UTC on 14 October. The resource shape was:

```yaml
resources:
  requests:
    cpu: "3500m"
  limits:
    cpu: "4"
```

This is a runner container resource excerpt, not a complete scale set manifest
or an agentihooks environment setting. Placement, capacity and resource
changes belong in reviewed antoncore configuration and CI delivery.

| Event around CPU request change | Completed gates / cohort before → after | Gate p50 | Gate p90 | Slowest unit wall p50 | Unit pickup p90 | Worker image wall p90 |
|---|---|---|---|---|---|---|
| Pull request | 26 / 26 → 17 / 17 | 1850 → 1854 | 5372 → 2675 | 558 → 395 | 2276 → 913 | 524 → 465 |
| Merge queue | 31 / 31 → 58 / 58 | 2186 → 1805 | 4178 → 2951 | 542 → 396 | 1485 → 1209 | 725 → 516 |

Successful gates increased from zero to four pull requests and ten merge
groups in these CPU comparison cohorts. The master's earlier seven-run sample
from 11:45–12:10 still had every run failing, mostly at the worker-count
dependent shard check exactly once step, tracked separately as watched task
t639. Faster execution does not imply that test partition failures are fixed.

### Inspect and reproduce the measurement

The published source issue contains the complete output. On the original
Doctor workspace, its retained scripts reproduce the report:

```bash
cd "$HOME/.agentihooks/swarm/rig-grade-swarm-doctor/tasks/t195"
python3 -I measure.py
python3 -I report.py
python3 -I close-proof.py
```

These are task evidence scripts, not installed product commands. They require
the original retained workspace and authenticated GitHub access; the runner
wait evidence additionally depends on historical cluster metrics. The source
also ran the original runner wait reader for both placement windows. For a
new comparison, record explicit UTC boundaries, exclusions, placement revision
and application time, and preserve raw attempt-specific responses. Keep the
same estimator on both sides and report missing evidence rather than zero.

Inspect GitHub records without running local tests:

```bash
gh api --method GET \
  repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs \
  -f event=pull_request \
  -f created='2026-10-13T13:10:34Z..2026-10-14T01:10:34Z' \
  -f per_page=100 --paginate \
  --jq '.workflow_runs[] | {id,event,created_at,run_started_at,run_attempt}'
run_id=38417922463
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$run_id/attempts/1"
gh api --paginate \
  "repos/the-cloud-clockwork/agentihooks/actions/runs/$run_id/attempts/1/jobs" \
  --jq '.jobs[] | {name,conclusion,created_at,started_at,completed_at,runner_name,labels}'
```

Repeat the listing with `event=merge_group`. The `created` search is a
discovery filter, not the cohort rule: widen it to catch runs created before
the window whose first attempt starts inside it, then select by the first
attempt's start time. Split busy searches into smaller UTC ranges if the API
reaches its 1000-result search ceiling. Do not substitute latest-attempt job
timestamps for attempt 1 or silently drop runs without a completed gate.

| Symptom | Inspection |
|---|---|
| Gate p90 falls while merges still fail | Compare successful-only gates and missing counts; inspect failure conclusions. |
| Runner wait rises | Separate cap wait, node provisioning and boot; inspect the allocation and pending reason before attributing it to OCI. |
| Runner contact disappears during node removal | Classify the infrastructure burst separately from assertion failures. |
| Historical wait is negative or a metric window is absent | Preserve the evidence gap; verify attempt identity and metric retention before computing another comparison. |
| Unit execution improves but gates stay red | Inspect the shard check and test partition, separately from runner CPU resources. |

## Unit runners steered off batch runner nodes

Pull request unit runner pods prefer nodes that run no batch runner pod. The
`anton`, `anton-oss` and `anton-oss-queue` runner scale sets carry a preferred
pod anti affinity, weight 100 per node, against pods of the `anton-oss-batch`
and `anton-batch` scale sets. Batch runners are never refused a node by it, and
the batch lane keeps its limits: `anton-oss-batch` still has `maxRunners` 30
and a three CPU runner limit. [antoncore pull request 973](https://github.com/the-cloud-clockwork/antoncore/pull/973)
delivered the rule; ArgoCD applied it at 01:23:52 UTC on 20 October 2026.

### Cause

[Issue 4958](https://github.com/the-cloud-clockwork/agentihooks/issues/4958)
found on 19 October that slow Python 3.12 pull request unit shards were
starved of CPU on runner nodes packed with batch and queue runner pods.
Co-tenant runners used 7.7 and 8.2 cores, the slow runner pods got 1.6 to 1.9
cores, and the slow shards ran a median 2.9 times slower per test. Their
split, test counts and worker counts matched the fast shards; every test ran
about three times slower than its stored duration.

### The rule

The term sits in the runner pod template of the antoncore runner charts:
`k8s/charts/arc-runners-oss/values-oss.yaml` for `anton-oss`, and
`k8s/charts/arc-runners/values.yaml` for its private twin `anton`, which a
twin test keeps identical. The `arc-runners-oss-queue` ArgoCD application
renders `values-oss.yaml` before `values-oss-queue.yaml`, so `anton-oss-queue`
inherits the term.

```yaml
gha-runner-scale-set:
  template:
    spec:
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                namespaces: ["arc-runners-oss", "arc-runners"]
                labelSelector:
                  matchExpressions:
                    - key: actions.github.com/scale-set-name
                      operator: In
                      values: ["anton-oss-batch", "anton-batch"]
```

This is an excerpt. The same list keeps its existing weight 100 term against
`qitp-worker` pods in `anton-prod`. Change the rule only through an antoncore
pull request into `dev`; ArgoCD tracks `dev` for these applications.

`tests/unit/arc_runners_oss/test_batch_separation.py` in antoncore renders each
of the three ArgoCD applications and asserts:

| Test | Asserts |
|---|---|
| `test_unit_runners_prefer_nodes_without_batch_runners` | each unit set has a weight 100 preferred term per hostname against both batch sets in their namespaces |
| `test_batch_runners_are_never_refused_a_unit_runner_node` | no unit set has a required term that names a batch set |
| `test_batch_lane_keeps_its_limits` | `anton-oss-batch` keeps `maxRunners` 30 and a CPU limit of `"3"` |

### Why the rule is preferred, not required

The first fix, [antoncore pull request 968](https://github.com/the-cloud-clockwork/antoncore/pull/968),
used `requiredDuringSchedulingIgnoredDuringExecution`. It merged at 23:10:28
UTC on 19 October. kube-scheduler also checks the required anti affinity of
pods already on a node against an incoming pod, so a required term on unit
runners repels batch runners from every node that holds a unit runner. Every
`anton-oss-batch` pod then stayed Pending, with scheduler events naming nodes
that failed existing pods' anti affinity rules.
[Pull request 970](https://github.com/the-cloud-clockwork/antoncore/pull/970)
reverted it at 00:30:34 UTC on 20 October, and pull request 973 replaced it
with the preferred term. Do not reintroduce a required term against batch sets; the
second test above fails on one.

A preferred term is one scheduling score among others, so on a busy cluster
a unit runner can still land beside a batch runner. Right after the apply,
three unit runner pods shared node `anton-oci-vjnhihp4znsq` with batch
runners. Since [antoncore pull request 994](https://github.com/the-cloud-clockwork/antoncore/pull/994),
merged at 11:16:51 UTC on 20 October, unit and batch runners schedule through
the `runner-binpack` scheduler profile. That profile scores
`NodeResourcesFit` with the `MostAllocated` strategy at weight 10 and disables
`NodeResourcesBalancedAllocation` and `PodTopologySpread`; it leaves
`InterPodAffinity`, which scores this term, at its default.

### Measured result

The number moved is the share of Python 3.12 pull request unit shards that
ran over nine minutes, 540 seconds. Both windows are twelve hours and use the
same script. The after window starts at the apply time.

| Window, UTC | Shards | Median s | p90 s | Over 540 s | Share |
|---|---|---|---|---|---|
| Before: 19 Oct 10:50:51 to 22:50:51 | 852 | 382 | 466 | 58 | 0.068 |
| After: 20 Oct 01:23:52 to 13:23:52 | 738 | 357 | 410 | 2 | 0.003 |

The before window closed twenty minutes before pull request 968 merged, so neither rule
was live in it; the required rule, live from 23:11:01 UTC until its revert,
falls in neither window. Two runs on each side returned no job list
and were skipped, as the source task's recorded proof states for each side. The last two hours of the after window ran under the
`runner-binpack` scheduler: recomputed from the retained after jobs, 690
shards started before pull request 994 merged, and both shards over 540 s are among them.

The Doctor finding that started this work was
`ci-tier-wall-time/39326180375-1`, judged at 1033 seconds while pull request
Tests run [39326180375](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39326180375)
was in progress. That run ended `cancelled` at 21:36:29 UTC on 19 October.
The measure refuses a completed run whose conclusion is neither `success` nor
`failure`; it still measures a run in progress against the current time. It
now refuses this finding:

```console
$ agentihooks doctor rig-grade-swarm measure ci-tier-wall-time/39326180375-1
doctor: run 39326180375 attempt 1 has no tier wall time
```

When the source task measured it, the older measure printed
`ci-tier-wall-time/39326180375-1 0`. Neither reading compares before and
after; the shard share above is the measured result.

### Check the rule and repeat the measurement

Read the rule on the live scale sets, read only:

```bash
for set in anton-oss anton-oss-queue; do
  kubectl get autoscalingrunnerset "$set" -n arc-runners-oss \
    -o jsonpath='{.spec.template.spec.affinity.podAntiAffinity}{"\n"}'
done
kubectl get autoscalingrunnerset anton -n arc-runners \
  -o jsonpath='{.spec.template.spec.affinity.podAntiAffinity}{"\n"}'
```

Each prints a `preferredDuringSchedulingIgnoredDuringExecution` list holding
the batch term and no `requiredDuringSchedulingIgnoredDuringExecution` key.
Check a running unit runner and its node. Pick a pod name from
`kubectl get pods -n arc-runners-oss -l actions.github.com/scale-set-name=anton-oss`:

```bash
pod="${UNIT_RUNNER_POD:?set to a unit runner pod name}"
kubectl get pod "$pod" -n arc-runners-oss -o jsonpath='{.spec.affinity.podAntiAffinity}{"\n"}'
node=$(kubectl get pod "$pod" -n arc-runners-oss -o jsonpath='{.spec.nodeName}')
kubectl get pods -A --field-selector "spec.nodeName=$node" \
  -l 'actions.github.com/scale-set-name in (anton-oss-batch,anton-batch)'
kubectl get events -n arc-runners-oss --field-selector reason=FailedScheduling
```

To repeat the share for a new window, list the pull request Tests runs created
in it and read their `unit (3.12` jobs. A shard counts when it started inside
the window, did not end `skipped` or `cancelled`, and ran at least 120
seconds. This prints the same figures as the table above:

```bash
start='2026-10-20T01:23:52Z'
end='2026-10-20T13:23:52Z'
filter='.jobs[] | select(.name | startswith("unit (3.12"))
  | select(.started_at != null and .completed_at != null)
  | select(.conclusion != "skipped" and .conclusion != "cancelled")
  | select(.started_at >= "'"$start"'" and .started_at < "'"$end"'")
  | (.completed_at | fromdate) - (.started_at | fromdate)
  | select(. >= 120)'
gh api --method GET --paginate \
  repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs \
  -f event=pull_request -f created="$start..$end" -f per_page=100 \
  --jq '.workflow_runs[].id' |
while read -r run; do
  gh api --paginate \
    "repos/the-cloud-clockwork/agentihooks/actions/runs/$run/jobs?per_page=100&filter=all" \
    --jq "$filter"
done | sort -n | awk '
  { d[NR] = $1; if ($1 > 540) over++ }
  END {
    if (NR == 0) { print "shards 0"; exit }
    median = NR % 2 ? d[(NR + 1) / 2] : (d[NR / 2] + d[NR / 2 + 1]) / 2
    printf "shards %d median %.0f p90 %d over540 %d share %.3f\n",
      NR, median, d[int(NR * 0.9) + 1], over, over / NR
  }'
```

The source task ran the same rule, with the same median and p90 picks, as
`measure.sh` in its Doctor task folder; that script is task evidence, not an
installed command. The `created` filter selects runs by creation time while
the shard rule uses job start time, so a run created shortly before `start`
can hold shards inside the window. The table used `start` as written; to
include those shards, move only the `created` lower bound a few hours earlier
and keep `start` in the filter.

| Symptom | Inspection |
|---|---|
| Batch runner pods stay Pending after a runner values change | Read `FailedScheduling` events; a required anti affinity on unit runners against batch sets causes this, as pull request 968 did |
| Unit shards slow again while the rule is live | Read node CPU for co-tenant runners in Prometheus; the preferred term can lose to packing on a full cluster |
| The rule is missing from a scale set | Check that the ArgoCD application synced the `dev` revision that carries it |
| A share is far off with few shards | Check for runs that returned no job list and for an unequal window length |

### Delivery evidence

- Spec: [antoncore issue 972](https://github.com/the-cloud-clockwork/antoncore/issues/972),
  after [issue 967](https://github.com/the-cloud-clockwork/antoncore/issues/967)
  for the reverted required rule and [issue 969](https://github.com/the-cloud-clockwork/antoncore/issues/969)
  for the revert.
- Red: antoncore CI Validate run [39352302110](https://github.com/the-cloud-clockwork/antoncore/actions/runs/39352302110),
  six failures, all in `test_unit_runners_prefer_nodes_without_batch_runners`.
- Green: CI Validate run [39352529232](https://github.com/the-cloud-clockwork/antoncore/actions/runs/39352529232),
  attempt 2. Attempt 1 lost only a k3s case to a kubectl exit 137 during
  kubelet start.
- Merged [pull request 973](https://github.com/the-cloud-clockwork/antoncore/pull/973)
  at 01:23:20 UTC on 20 October; ArgoCD synced
  `arc-runners-oss-queue` at 01:23:36 and `arc-runners` and `arc-runners-oss`
  at 01:23:52.
- Read only checks after the apply: new template pods on all three unit sets
  carried the preferred term and no required term, four `anton-oss-batch` pods
  scheduled at creation, and no batch `FailedScheduling` event followed.

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

## Mutation preflight legs on the batch pool

The `stats` and `mutation-shard` legs of the Mutation preflight workflow hold no lane. Each leg's concurrency group names its own run id and matrix index, `mutation-preflight-job-<run id>-stats-<part>` or `mutation-preflight-job-<run id>-shard-<shard>`, so no other run ever shares it. A ready leg waits only for a free runner on the `anton-oss-batch` runner scale set, whose ceiling of 30 runners caps how many legs run at once across every branch. The ceiling is the `maxRunners` value in antoncore's `arc-runners-oss` chart, `values-oss-batch.yaml`. Every planned mutant and every covering test still runs; only the queueing changed.

Grouping per branch stays at the workflow level. A branch push run joins `mutation-preflight-refs/heads/<branch>` with `cancel-in-progress`, so a newer push to the same branch cancels the older run, and a push to another branch never waits on it. When Tests or Proofs calls the workflow, the legs take the caller's run id in the same per run form.

| Job | Runner label | Concurrency group |
|---|---|---|
| Whole run, branch push | per job, below | `mutation-preflight-refs/heads/<branch>`, newer push cancels the older run |
| `plan`, `refine`, `mutation` | `anton-oss-light` | the run's group only |
| `touched-tests` | `anton-oss` | the run's group only |
| `stats` | `anton-oss-batch` | `mutation-preflight-job-<run id>-stats-<part>` |
| `mutation-shard` | `anton-oss-batch` | `mutation-preflight-job-<run id>-shard-<shard>` |

The runner labels apply to runs from the trusted actors the workflow lists and to merge queue runs; any other run takes `ubuntu-latest`.

### Branch workflow lanes compatibility

The `Branch workflow lanes` workflow grades branch pushes using the grader from
`dev`; it fetches the pushed head as data. It excludes pushes to `dev`, `main`
and merge queue branches. Its `grade` job checks new or edited workflows, so an
older branch can be refused even when `Gate Required` passes. Read both checks.

The Mutation preflight exception accepts the current per run groups and the exact
superseded three lane expression. The latter keeps branches cut before the batch
pool change gradeable until they incorporate `dev`; it does not move current
jobs back onto shared lanes or remove the batch pool ceiling.

For maintainers changing the existing jobs, these are the current job level
`concurrency` blocks. Keep the jobs' existing runner expressions, matrices and
other configuration:

{% raw %}
```yaml
jobs:
  stats:
    concurrency:
      group: ${{ format('mutation-preflight-job-{0}-stats-{1}', github.run_id, matrix.part) }}
  mutation-shard:
    concurrency:
      group: ${{ format('mutation-preflight-job-{0}-shard-{1}', github.run_id, matrix.shard) }}
```
{% endraw %}

Neither block has `queue` or `cancel-in-progress`. Workflow level cancellation
of older pushes stays in effect. No new environment variable or operator toggle
enables compatibility; the trusted grader supplies it automatically.

The accepted legacy `stats` block below is a compatibility example, not a
recommended configuration for new work:

{% raw %}
```yaml
concurrency:
  group: ${{ format('{0}-stats-{1}', github.workflow == 'Mutation preflight' && format('mutation-lane-{0}', ((endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || endsWith(github.run_id, '2')) && 'a') || ((endsWith(github.run_id, '3') || endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b') || 'c') || format('mutation-preflight-job-{0}', github.run_id), matrix.part) }}
  queue: ${{ github.workflow == 'Mutation preflight' && 'max' || 'single' }}
```
{% endraw %}

For the legacy `mutation-shard` block, the stage is `shard` and the index is
`matrix.shard`; every other part of the expression and queue policy is identical.
Under the workflow's own name, these legacy blocks select `mutation-lane-a`,
`mutation-lane-b` or `mutation-lane-c` with `queue: max`. Under a caller's name,
they select the caller's per run group with `queue: single`.

This exception has a narrow boundary:

| Requirement | Accepted boundary |
|---|---|
| Trusted workflow | The same workflow path exists in the trusted baseline, with matching workflow name and event configuration; its name is `Mutation preflight` |
| Job | Only the baseline's existing `stats` and `mutation-shard` jobs qualify |
| Runner | The job's `runs-on` expression equals the baseline's expression, including its hosted fallback |
| Group | The entire concurrency mapping equals the current per run block or the superseded block for that job |
| Other branch workflows | A copied preflight, renamed job or branch only job gains no mutation exception; the ordinary lane rules still apply |

A different stage, matrix index, lane selection, queue policy or an added
`cancel-in-progress` field fails this exception. A branch only job with no
accepted job or workflow level lane group is still refused with
`job <name> has no shared hosted lane concurrency group`.

### Before the change

Until pull request [#4487](https://github.com/the-cloud-clockwork/agentihooks/pull/4487), the two legs shared three mutation lanes, `mutation-lane-a`, `mutation-lane-b` and `mutation-lane-c`, picked from the last digit of the run id like the hosted lanes, with `queue: max`. A lane held one stats leg and one shard leg per matrix index, so a ready shard queued behind the shard of the same index from another branch whose run id ended in the same range, even while batch runners sat free. In [run 39086455436](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39086455436), shard 2 was created at 01:32:17 and started at 01:45:36, a 799 second wait; the investigation found 794 seconds of it before its runner pod was created, while pods on the batch pool were created, scheduled and ready within two seconds of being asked. Pull request #4487 superseded [#4300](https://github.com/the-cloud-clockwork/agentihooks/pull/4300), which computed the lane count from the ceiling and stage width and still gave three lanes for ten shards and a ceiling of 30.

### Check a run

List each shard's creation and start time:

```bash
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs --paginate \
  --jq '.jobs[] | select(.name | startswith("mutation-shard")) | [.name, .created_at, .started_at] | @tsv'
```

A shard that started within seconds of its creation did not queue. When shards wait for minutes, read the batch runner set's load:

```bash
kubectl get autoscalingrunnerset anton-oss-batch -n arc-runners-oss
```

Current runners at the 30 maximum means the pool ceiling holds the shards, which is the intended limit. Current runners below the maximum with shards still queued points to runner provisioning, not to concurrency. The concurrency groups themselves live in `.github/workflows/mutation-preflight.yml`:

```bash
grep -n -A1 'concurrency:' .github/workflows/mutation-preflight.yml
```

### Measured result

Shard pickup is a shard job's `started_at` minus its `created_at`, read for every branch push run of the workflow in a 24 hour window, counting shards that ran (not skipped or cancelled). The before window is the day up to the merge of #4487 at 16:30 UTC on 18 October; the after window is 21 October UTC.

| Window | Push runs | Runs with shards | Shard jobs | Median | 90th percentile | Max | Over 600 s | 799 s or more |
|---|---|---|---|---|---|---|---|---|
| 17 Oct 16:30 to 18 Oct 16:30 UTC | 1266 | 221 | 1428 | 11 s | 816 s | 2644 s | 210 | 147 |
| 21 Oct 00:00 to 22 Oct 00:00 UTC | 615 | 114 | 525 | 12 s | 150 s | 501 s | 0 | 0 |

No shard in the after window waited as long as the 799 second case. A push run uses the workflow file on its own branch, so a branch cut before the merge still queued on the old lanes until it took `dev`. The script that produced these rows pages the workflow's push runs in two hour slices, then reads each run's jobs with the shard command above.

### Mutation shard troubleshooting

| Symptom | Check |
|---|---|
| A shard queues for minutes while other branches run mutation | Read the batch runner set's current and maximum runners; at the maximum, the ceiling is the limit |
| A branch's older preflight run disappears | A newer push to the same branch cancelled it; read the newer run |
| A shard runs on `ubuntu-latest` | The actor or triggering actor is not on the workflow's trusted list |
| Branch workflow lanes refuses `stats` or `mutation-shard` | Check the trusted workflow name, events and runner expression, then compare the entire concurrency mapping with the current or exact legacy block above. Incorporating `dev` restores the current per run groups |
| Gate Protected refuses a workflow edit | Read that separate gate's diagnostic; a passing branch lane grade does not establish that every workflow protection passed |
| A branch only job without a lane group is refused | Give it an accepted shared hosted lane using an existing hosted workflow as the model; copying a mutation job's per run group does not qualify it for the exception |
| The lanes contract test fails after a workflow edit | A leg's group no longer names its own run id, or the leg left `anton-oss-batch`; restore the per run group |

`tests/test_ci_hosted_lanes.py` holds the contract: `test_mutation_legs_of_two_branches_never_share_a_concurrency_lane_and_stay_on_the_batch_pool` resolves both legs for two branches and asserts two distinct groups, none of them a lane, and the `anton-oss-batch` label; `test_a_branch_mutation_job_never_holds_a_lane_that_proofs_or_dispatches_wait_on` asserts the per run group for ten run ids and three indexes. In the red Tests run [39123433688](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39123433688), the `unit-selected (3.11, 2)` job failed the new contract test with both branches on `mutation-lane-a`; the green Tests run [39136265932](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39136265932) passed after the branch lane grader accepted per run groups in [#4491](https://github.com/the-cloud-clockwork/agentihooks/pull/4491).

Pull request [#4576](https://github.com/the-cloud-clockwork/agentihooks/pull/4576)
then added legacy block compatibility. Its regression cases cover both jobs,
altered queue, cancellation, stage, index, lane and runner, changed events,
swapped blocks and a branch only copy. The original refusal of a job without a
lane group remains covered. The delivery issue records two refusals among eight
branch pushes in its initial sample; this is narrower than the source task's
claim that every push failed. The Doctor's applied check reports all first six
branch lane runs after the merge passed, including
[39154101511](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39154101511).

To diagnose the branch grade separately from shard pickup, inspect its push run:

```bash
gh run list --workflow branch-lanes.yml --branch <branch> --limit 5
gh run view <run id> --log-failed
```

For a grader regression, run the existing contract tests in CI after pushing:

```bash
gh workflow run proofs.yml --ref <branch> -f proof=tests \
  -f tests='tests/test_ci_branch_lanes.py'
gh run list --workflow proofs.yml --branch <branch> --limit 5
gh run view <proof run id> --log
```

Keep the no group refusal control alongside the accepted current and legacy
cases. A green grade proves workflow admission; it does not prove that mutation
tests passed or that a runner was immediately available. Run no tests on the
shared development host.

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
