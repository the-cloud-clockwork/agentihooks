# Sonar quality gate

The `sonar` job in the Tests workflow is a pull request requirement of `Gate — Required`, except on a [test only pull request](#test-only-pull-requests). It scans the current pull request head and fails when the SonarQube quality gate fails. The merge queue runs the unit suite and test count checks; it does not repeat Sonar or the other pull request checks. Promotion owns the slower integration checks, and a red promotion holds release while `dev` keeps merging. See [Delivery tiers](/docs/reference/delivery-tiers/) for the three tiers.

agentihooks is bound to the `Delivery L2` gate, defined and bound in antoncore's SonarQube stack config (`stacks/sonarqube/config/gates/delivery-l2.json` and `projects.json`). Its conditions on new code:

| Metric | Fails when |
|---|---|
| Coverage | below 80 % |
| Bugs | above 0 |
| Vulnerabilities | above 0 |
| Duplicated lines | above 3 % |
| Security hotspots reviewed | below 100 % |

When the gate fails, the `SonarQube Quality Gate` step prints its error line, then every page of unresolved new bugs and vulnerabilities and new security hotspots still to review for the branch or pull request it graded. Each finding occupies one line with component, line or zero, rule and message. The searches use the existing read token through the job's Access proxy; task and gate polling keep the scan token. Both lists and all retries share one thirty second timeout. A refused or empty later page leaves earlier findings visible and reports the remaining count when known. A timeout can end the read before that count prints. Diagnostic failures preserve the original error and failed exit.

The job's `Hold the Delivery L2 conditions` step runs after the quality gate step and fails when:

- the `Delivery L2` gate on the server holds conditions other than the five above, so a loosened threshold turns the job red;
- the analysis was graded on a condition outside those five, as after a rebind to another gate;
- bugs or vulnerabilities were not graded.

Sonar leaves a condition out of the result when its metric has no value on the change (no new coverable lines, no hotspots), and skips coverage and duplication on a change under 20 new lines; a planted fault must therefore add at least 20 coverable lines. The scan token cannot read the project binding itself (HTTP 403 on `api/qualitygates/get_by_project`), so a rebind to a gate whose conditions are a subset of these five passes the step; the binding is kept by antoncore's `projects.json`, applied on every SonarQube stack deploy. Gate thresholds change only through the antoncore config.

## Test only pull requests

A pull request into `dev` whose every changed path sits under `tests/` skips the `sonar` job. `sonar-project.properties` excludes `tests/**` from the sources Sonar grades, so such a change has no new code to grade, and the empty job cost about 13 seconds of runner pickup and 21 seconds of run before `Gate — Required` could start.

The `select` job decides in its `Select the tests the change reaches` step. On an event other than `pull_request`, or when the grader checkout lacks `scripts/ci_select.py`, the step writes only `scope=full`, `legs=[]` and `count=0` and exits, so no `sonar` output exists. Otherwise it lists the paths that differ between the pull request's base commit and the merge commit under test and writes `sonar` before `scripts.ci_select` writes the scope, legs and count outputs. The `sonar` part of the step:

```bash
git -C "$GITHUB_WORKSPACE" diff --no-ext-diff --no-renames --name-only -z "$PR_BASE" "$GITHUB_SHA" > "$RUNNER_TEMP/changed-paths"
sonar=run
if [[ -s "$RUNNER_TEMP/changed-paths" ]]; then
  sonar=skip
  while IFS= read -r -d '' path; do
    [[ "$path" == tests/* ]] || sonar=run
  done < "$RUNNER_TEMP/changed-paths"
fi
echo "sonar=$sonar" >> "$GITHUB_OUTPUT"
```

| Change | `select.outputs.sonar` | `sonar` job | `Gate — Required` on `sonar` |
|---|---|---|---|
| Pull request touching only `tests/` | `skip` | skipped | must be skipped |
| Pull request touching any path outside `tests/`, such as `docs/` or `.github/` | `run` | runs | must succeed |
| Pull request with an empty diff | `run` | runs | must succeed |
| Pull request whose grader lacks `scripts/ci_select.py` | empty | runs | must succeed |
| Merge group | empty | skipped as before | must be skipped |
| Dev push or dispatch | empty | runs | must succeed |

`--no-renames` lists both sides of a rename, so moving a file from `scripts/` into `tests/` still runs Sonar. The `sonar` job's `if` adds `needs.select.outputs.sonar != 'skip'`; only the exact value `skip` turns it off.

`Gate — Required` removes `sonar` from the pull request's required job list only when `select.outputs.sonar` is `skip`. Every job outside the required list must be skipped, so on a test only pull request a `sonar` that ran turns the gate red, and on any other pull request a skipped `sonar` turns it red. The gate's aggregation step is a protected form: the `Gate — Protected` check runs the base branch's `scripts.gate_protected`, which admits the step either unchanged or as rendered by `scripts.ci_reuse.test_only_sonar`, and admits a `select` job whose outputs gain only `sonar: ${{ steps.select.outputs.sonar }}`. The grader change was announced and merged before the workflow change, so pull requests already open kept a valid required check.

There is no setting. To make Sonar run on a test only change, touch a path outside `tests/`. To change which paths count as test only, edit the `select` step, the gate aggregation in `scripts.ci_reuse` and its admission in `scripts.gate_protected` together through reviewed pull requests.

### Checking a run

Read the `sonar`, `select` and `Gate — Required` jobs of the pull request's Tests run:

```bash
R=the-cloud-clockwork/agentihooks
PR=7852
SHA=$(gh pr view "$PR" -R "$R" --json headRefOid --jq .headRefOid)
RUN=$(gh api "repos/$R/actions/runs?head_sha=$SHA&event=pull_request" \
  --jq '[.workflow_runs[] | select(.name == "Tests")][0].id')
[[ -n "$RUN" && "$RUN" != null ]] || { echo "no Tests run for $SHA" >&2; exit 1; }
gh api "repos/$R/actions/runs/$RUN/jobs" --paginate \
  --jq '.jobs[] | select(.name == "sonar" or .name == "select" or .name == "Gate — Required" or (.name | startswith("unit-selected"))) | [.name, .conclusion, .started_at, .completed_at] | @tsv'
gh pr view "$PR" -R "$R" --json files --jq '.files[].path'
```

On pull request 7852, which changed only test files, run 40542648681 printed `sonar` as `skipped` and `Gate — Required` as `success`.

| Symptom | Cause and fix |
|---|---|
| `Every gate required for this event must succeed and other gates must be skipped.` with `sonar` skipped | The head's gate step still requires `sonar` while `select` wrote `skip`. Render the gate step from `scripts.ci_reuse` so it carries the test only form |
| Sonar skipped but the change touched code | Read the changed paths in the `select` step log; that list alone decides, and any path outside `tests/` writes `run` |
| `Gate — Protected` rejects the gate step | The head's aggregation differs from every rendering `scripts.gate_protected` admits. Render it from `scripts.ci_reuse` instead of editing it by hand |
| Gate still starts long after the selected tests | On a test only change Sonar is skipped, so the gate's start is set by the last other job it needs. Compare the gate's `started_at` with that job's `completed_at`; a gap with no job finishing is runner pickup |

### Test only delivery evidence

[Pull request #7580](https://github.com/the-cloud-clockwork/agentihooks/pull/7580) delivered the workflow change. Before it, [#7572](https://github.com/the-cloud-clockwork/agentihooks/pull/7572) announced the grader and [#7568](https://github.com/the-cloud-clockwork/agentihooks/pull/7568) merged the grader change; [issue #7567](https://github.com/the-cloud-clockwork/agentihooks/issues/7567) names the seams. Before the change, tests to required gate on test only pull requests had a median of 52 seconds with none of 12 runs under 40 seconds, over 25 October 17:21 to 26 October 15:49 UTC.

The first three test only runs after the merge skipped Sonar and passed `Gate — Required`:

| Run | Pull request | Scope | Last required job done | Gate started | Gap |
|---|---|---|---|---|---|
| 40539354877 | 7899 | full | 16:31:29 `shard-check (3.12)` | 16:34:23 | 174 s |
| 40540834415 | 7847 | selected | 16:32:51 `unit-selected (3.12, 1)` | 16:35:38 | 167 s |
| 40542648681 | 7852 | selected | 16:42:58 `unit-selected (3.12, 1)` | 16:45:21 | 143 s |

No required job finished inside those gaps, so on these samples the 40 second target was not met and the remaining wait is gate pickup. A median over a full window has not been recorded yet.

## False positives

An issue is marked false positive or won't fix only through the `sonar-config` skill's `issue-transition` helper, never by hand in the SonarQube UI. Two readers that did not write the flagged code each record their ruling as a comment on the Sonar issue before the transition; the pull request that needs it names both readers and the issue key.

## Shard coverage merge

The Sonar job flattens each shard's Python branch coverage to its distinct
executed arcs before combining reports. Per test context names describe which
test executed an arc; Sonar's coverage XML needs the union of those arcs.
Flattening removes that repeated context data from the scan's combined database
while retaining measured files, including files with no hits, and file tracer
metadata. It leaves the downloaded shard databases intact.

The merge produces `coverage.xml`, a TOTAL line naming the lines and branches
it covers, and `lcov.info`. `.github/coverage/parallel_xml.py` writes the XML
with one `coverage xml` process per part, as many parts as
`scripts/ci_cpu_quota.py` allows. It splits the measured files by directory, so
every package lands whole in one part, puts each directory on the part with
the fewest source bytes so far, and joins the parts sorted by package with the
root totals summed. The joined report carries the same files, packages, lines
and totals as one process's report. The merge prints no per file table; the
per file view is in Sonar and in the uploaded `coverage.xml`.
Python XML still covers `hooks` and `scripts`; JavaScript LCOV still includes
node and browser captures. The optimization changes coverage preparation only:
Sonar sensors, rules, scan inputs and Delivery L2 conditions remain enabled.

For full scope runs, `Build the test dependency manifest` independently combines
the original per test contexts with `--keep` into
`$RUNNER_TEMP/dep-manifest/.coverage`, outside the scanned checkout. The manifest
builder needs those contexts to associate tests with code. Using the flattened
scan database for that build would lose those associations.

### Taking coverage as shards land

The `sonar` job needs only `select`, `reuse`, `admission`, `lab-verdict` and
`collect`, so it starts beside the unit shards on a standard runner, after
`collect` has uploaded both reused test plan artifacts that its pattern
download fetches by one listing of the run. Its runner wait, checkout
and carried coverage finish while the shards run. The job holds that runner
while it waits, because a light runner requests a quarter CPU and ran the scan
three to four times slower. `Take each shard's coverage as it lands` runs
`.github/coverage/arrivals.py` from `actions/github-script` with
`github-token: none`. Every 3 seconds it lists the run's artifacts through the
results service, the same service `download-artifact` uses for the current run,
with the job's runtime token. The script holds no GitHub token, so its listing
spends no REST quota. On a push to `dev` a separate watcher reads job states
with an App token, as [Queued shards and the wait budget](#queued-shards-and-the-wait-budget)
describes. It downloads each shard's coverage artifact into
`.coverage-shards/<artifact name>` when the artifact is listed. While shards
still run, it flattens each database it holds into `flat-<name>` beside it.

A shard ends when its coverage is taken, or when its flake report is listed with
no coverage of it beside. `unit` and `unit-selected` upload the flake report after
their coverage, so a listed flake report means no more coverage is coming. A
rerun attempt counts only a flake report whose artifact changed since the
wait's first listing. Coverage that an earlier attempt left counts at once. A
shard that leaves neither coverage nor a flake report, such as one that lost
its runner or ran past its job timeout, holds the wait to its deadline, and so
does listed coverage that never downloads. The wait gives up once 44 minutes
of its budget are spent. On a push to `dev`, time while every unfinished shard
job still waits for a runner is not spent; on every other run each minute
counts. The wait never fails the job itself, so a shard still missing reaches
`combine.sh`, which names it.

`combine.sh` moves each `flat-<report>` it finds into its combine input and
flattens only the reports that have no flat copy. After the last shard lands,
only that shard's flatten, the combine and the XML parts remain before the
scan. A failed part fails the merge with its exit code and leaves no
`coverage.xml`.

`Merge shard coverage` runs in the background while the proxy starts.
`Wait for the coverage merge` must succeed before `SonarQube Scan` starts. The
manifest build also runs in the background, starts before the cache restores,
and is checked after the scan. Moving it off the scan's prerequisite path does
not make its success optional. Both waits have five minute timeouts.

### Queued shards and the wait budget

On a push to `dev` the twenty unit shards can wait for a runner longer than
they run. Before [pull request #12636](https://github.com/the-cloud-clockwork/agentihooks/pull/12636)
the coverage wait counted that queue time like run time, so shards that queued
for most of 44 minutes ended `sonar` red with no analysis, and the head could
not promote. On dev push run 44688839519 the wait gave up after 44 minutes and
1 second, while shard 15 landed 62 minutes and 6 seconds after the wait began.

Three steps around `Take each shard's coverage as it lands` keep queue time
out of the budget. They run only on a push to `dev` whose scope is not `none`
and whose source is not test only; the wait step itself runs on every run with
that scope and source:

| Step | What it does |
|---|---|
| `Mint the token that reads this run's job states` | Mints a GitHub App token, step id `jobs-token`, with `actions: read` on this repository. The job's own `permissions` stay `contents: read`. |
| `Watch whether this run's shard jobs have started` | Starts `.github/coverage/queue_watch.py` in the background with the job name prefix `unit (`, sends its output to `queue-watch.log` and records its process id. |
| `Take each shard's coverage as it lands` | Runs `arrivals.py`, which reads the watcher's answer from a file. |
| `Stop the queue watch` | Kills the watcher after the wait, whatever the wait's outcome. |

Every 30 seconds the watcher reads this run attempt's jobs from
`/repos/<owner>/<repo>/actions/runs/<run>/attempts/<attempt>/jobs?per_page=100`
and keeps the jobs whose name starts with the prefix. It sets aside each shard
job whose status is `completed`. If any remaining shard job has a status other
than `queued`, or none remain, it writes `started` to
`.sonar-shard-queue-state` in the checkout. If every remaining one is `queued`,
it writes `queued`. A failed read, or a page with no matching job name, writes
nothing, and the file keeps its last answer. The watcher stops after 55
minutes, inside the one hour life of the App token, because every read past
that life fails authorization. It reads one page of up to 100 jobs; a dev push
run lists 87, and shard jobs past the first page would not be seen.

`arrivals.py` holds no token and makes no API call for this. It rereads the
state file every 30 seconds. While its last read was a fresh `queued`, the 3
second pauses between artifact listings are added back to the budget, so that
time is not spent. Every other answer spends the time as before:

| State file | Spends the 44 minute budget |
|---|---|
| `queued`, written within the last 90 seconds | No |
| `started`, written within the last 90 seconds | Yes |
| Last written more than 90 seconds ago | Yes |
| Missing, unreadable or holding anything else | Yes |

The wait therefore counts wall time whenever the watcher has no fresh answer:
before the shard jobs appear in the run, while the jobs API fails, when the
watch steps did not run, and from about 56 minutes after the watcher started.
The budget pauses only while every unfinished shard job is queued. Once one
shard job runs, the wait counts again, even while other shards still queue.

Two wall clock limits still bound a dev push. The step's own timeout is 75
minutes from the step's start, whatever the budget says, and the `sonar` job's
timeout is 90 minutes. A wait can therefore leave out at most 31 minutes of
queue time before the step timeout ends it ahead of the budget. The step has
`continue-on-error`, so after either end `Stop the queue watch` runs and
`combine.sh` names each shard still missing.

Every run other than a push to `dev` starts no watcher. Its state file is
missing, so its wait counts wall time: 44 minutes inside a 45 minute step
timeout and a 60 minute job timeout. The workflow passes the prefix
`unit-selected (` for a selected scope, but `unit-selected` shards run only on
pull requests, where no watcher runs.

The watcher costs one jobs API read every 30 seconds from its start until
`Stop the queue watch` kills it, at most 110 reads, against the App
installation's rate limit.

### Workflow configuration

This behavior is automatic in Tests; it introduces no user setting or secret.
The workflow supplies the merge inputs:

| Input | Current meaning |
|---|---|
| `SHARDS` | Full scope uses 20; selected scope uses the selection job's shard count. Every numbered primary shard database must be present and nonempty. |
| `SCOPE` | `selected` passes `--selected` to the JavaScript LCOV builder and lets the coverage wait take `branch-coverage-3.12-<leg>-<attempt>`. Scope `none` skips the coverage wait and the merge; only `full` builds the dependency manifest. |
| Download layout | `.coverage-shards/coverage-3.12-<shard>/.coverage`; each shard's optional `reused-coverage/.coverage` is included when nonempty. A `flat-<name>` beside a database is its flat copy from the coverage wait. |
| Wait budget | `WAIT` in `arrivals.py`: 44 minutes of spent time. `INTERVAL` is the 3 second pause between artifact listings, `JOB_POLL` the 30 second reread of the queue state and `STALE` its 90 second freshness limit. |
| Wait step timeout | `Take each shard's coverage as it lands`: 75 minutes on a push to `dev`, 45 minutes on every other run. |
| Job timeout | `sonar`: 90 minutes on a push to `dev`, 60 minutes on every other run. |
| Queue state file | `.sonar-shard-queue-state`, `QUEUE_STATE` in `arrivals.py` and `STATE` in `queue_watch.py`, holding `queued` or `started`. |
| Queue watcher | `queue_watch.py` polls every 30 seconds (`INTERVAL`) for at most 55 minutes (`BOUND`), with a 30 second request timeout (`TIMEOUT`). Its prefix is `unit (`, or `unit-selected (` for a selected scope. |
| Coverage configuration | `.github/coverage/coverage.ini` enables branch coverage and relative filenames and includes `hooks/*` and `scripts/*` in reports. |
| Scanner report settings | `sonar.python.coverage.reportPaths=coverage.xml` and `sonar.javascript.lcov.reportPaths=lcov.info`. |
| Scan inputs | `sonar.exclusions` lists `coverage.xml`, `lcov.info`, `sonar-scope.txt` and `.coverage-shards/**`, the files this job writes into the checkout that `.gitignore` does not already drop, so the scan and its secret detection grade repository files only. Sonar still reads both reports from their report paths on disk. |

For example, the current full scope workflow invokes the merge as follows from
the checkout root, after downloading that run's artifacts:

```bash
SHARDS=20
SCOPE=full bash .github/coverage/combine.sh --downloaded "$SHARDS"
```

A selected scope run with three primary shard reports uses:

```bash
SHARDS=3
SCOPE=selected bash .github/coverage/combine.sh --downloaded "$SHARDS"
```

These illustrate CI invocations, not commands to run on the shared development
host. Supply the actual shard count for that run; the original delivery measured
twelve shards. Change shard counts, report configuration and scan settings
through the reviewed workflow and configuration sources together. There is no
flag to flatten less data or disable a sensor for speed.

Change the wait budget, the timeouts and the watcher settings together. The
watcher's bound must stay under the App token's one hour life, and the step
timeout must stay under the job timeout with room for the merge and the scan.
`tests/test_ci_sonar_workflow.py` pins the job timeout,
`tests/test_sonar_coverage.py` keeps the watcher's bound under an hour, and
`tests/test_sonar_arrivals.py` and `tests/test_queue_watch.py` cover the queue
state answers.

### Inspect coverage and startup timing

Inspect an existing Tests run rather than launching tests locally. Replace the
example run id with the run being investigated:

```bash
RUN_ID=39060292125
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$RUN_ID/jobs" \
  --paginate \
  --jq '.jobs[] | select(.name == "sonar") | {id, started_at, conclusion, html_url}'
JOB_ID=117479092946
gh api "repos/the-cloud-clockwork/agentihooks/actions/jobs/$JOB_ID/logs"
```

Replace the job id with the `id` returned for that run. Subtract the job's UTC
`started_at` from the scanner's embedded timestamp on the first `INFO` sensor
line in `SonarQube Scan`. The outer Actions timestamp can reflect buffered log
delivery. For this example, `19:09:06.044` minus `19:08:04Z` is 62.0 seconds;
the outer line timestamp is `19:10:28.3286316Z`. Retain the run URL, revision,
attempt, scope and interval.
Compare successful first Sonar attempts on dev push runs with the same scope;
report sample counts and medians. Queue delay, the scanner step duration and
whole job duration are separate measurements. A skipped scan supplies no first
sensor sample. Exclude superseded analyses from the successful cohort even when
they reached a sensor before the quality gate reported supersession.

To see how long each unit shard queued, read the run attempt's jobs. A job's
`created_at` to `started_at` is its queue time, and `started_at` to
`completed_at` its run time:

```bash
RUN_ID=44943101327
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$RUN_ID/attempts/1/jobs?per_page=100" \
  --jq '.jobs[] | select(.name | startswith("unit (")) | [.name, .created_at, .started_at, .completed_at] | @tsv'
```

The same listing holds the `sonar` job's steps with their start and end
times, which show whether `Mint the token that reads this run's job states` and
`Watch whether this run's shard jobs have started` ran. The watcher writes
nothing to the step log; its errors go to `queue-watch.log` on the runner,
which no step uploads.

Each `<artifact> landed N s into the wait` line of `Take each shard's coverage
as it lands` counts spent budget seconds. On a push to `dev` it leaves out the
paused queue time, so N can be smaller than the time since the step started.
The `had not ended after 44 minutes` warning also counts budget minutes. On
run 44943101327 the step started at 20:13:20 UTC and shard 8 landed at
20:15:56, which its line logged as 75 s into the wait instead of 156: about
81 seconds of queue time were not spent.

`Wait for the coverage merge` prints the background merge log and coverage
table. `Wait for the test dependency manifest` prints its separate build log.
The `sonar-report` artifact retains the XML, LCOV and scanner task receipt for
seven days; full scope runs also retain `dep-manifest/manifest.json` as the
`dep-manifest` artifact for seven days. For example:

```bash
gh run download "$RUN_ID" \
  --repo the-cloud-clockwork/agentihooks \
  --name sonar-report \
  --dir ./sonar-evidence
```

| Failure or symptom | Inspection |
|---|---|
| `Missing coverage for shard N` | Read `Take each shard's coverage as it lands` for the artifacts it logged as landed, and compare the expected count with them; an optional reused report does not replace the required primary database. |
| `Shards N had not ended after 44 minutes` | The coverage wait spent its 44 minute budget on shards that never uploaded coverage or a flake report, such as a shard that lost its runner. On a pull request it counts the time a shard waited for a runner too. On a push to `dev` that time is left out only while every unfinished shard job is queued. Read those shard jobs' queue and run times. A rerun of `sonar` alone takes any coverage they left at once and waits to the deadline again for a shard that left none. |
| Dev push wait spent time while its shards queued | The watcher had no fresh answer. Check that `Mint the token that reads this run's job states` and `Watch whether this run's shard jobs have started` ran, whether the shard jobs existed yet by their `created_at`, and whether the queue outlasted the watcher's 55 minutes. A missing or stale state file spends the time by design. |
| `Take each shard's coverage as it lands` ends at 75 minutes on a dev push | The step timeout ended the wait before its budget ran out, which takes more than 31 minutes of skipped queue time. The job goes on and `combine.sh` names each shard still missing. Read those shard jobs' `created_at` and `started_at`. |
| Coverage merge fails or ends without a status | Read the log printed by the merge wait for flattening, combine, XML or LCOV errors. Keep the wait and all coverage inputs. |
| Merge wait times out | Inspect the merge process and runner resource timings; the short background launch step does not measure merge completion. |
| No browser or node coverage | Inspect the shard capture artifacts and the LCOV diagnostic; retain both input types required for that scope. |
| Dependency manifest fails | Read its separate wait log and confirm the build consumed the context-bearing shard databases, not the scan's flattened database. |
| Startup remains slow after merge completion | Compare cache and proxy setup completion with the first sensor timestamp; the merge optimization does not bound those other costs. |

### Delivery evidence

[Pull request #4147](https://github.com/the-cloud-clockwork/agentihooks/pull/4147)
delivered the flat merge. Its
[green Tests run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39051370669)
validated the change, including regression coverage for identical line and
branch hits, zero-hit files and retained source contexts. The delivered profile
on twelve reports from run `39048985841` reduced combine work from 16.2 seconds
to 2.8 seconds including flattening, and XML generation from 16.8 to 12.6
seconds. The XML and report table matched the old merge apart from the XML
timestamp.

The detailed source task proof (`t335` in the Doctor ledger) records matched dev
push cohorts measured from job start to first sensor on successful first Sonar
attempts:

| Cohort | Samples | Median first sensor | Median Sonar job |
|---|---|---|---|
| Before: `39041846700` through `39048985841` | 13 | 67.4 seconds | 176 seconds |
| After #4147: `39054086963` through `39060292125` | 10 | 57.3 seconds | 171 seconds |

That proof records Sonar new code coverage at 98.4% with gate OK on three runs
from each cohort. It also records a scan step median increase from 97 to 115
seconds. The subsequent
[pull request #4188](https://github.com/the-cloud-clockwork/agentihooks/pull/4188)
started the manifest before the cache restores; its first recorded dev run
`39061917710` reached the first sensor in 56.3 seconds with a 94 second scan.
The [source issue](https://github.com/the-cloud-clockwork/agentihooks/issues/4145)
summarizes both changes. These historical measurements establish the delivered
improvement; current shard counts and runner load can produce different times.

[Pull request #12636](https://github.com/the-cloud-clockwork/agentihooks/pull/12636)
made the coverage wait skip queued shard time on pushes to `dev`. Dev push
[run 44688839519](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44688839519),
on the earlier code, showed the fault: its wait gave up after 44 minutes and
1 second while shard 15 landed 62 minutes and 6 seconds after the wait began.
[Run 44694005666](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44694005666)
turned every new wait test red on the code before the change. On dev push
[run 44943101327](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44943101327)
all four steps ran: the twenty shard jobs were created at 20:13:33 UTC and
started between 20:14:59 and 20:17:13, the last shard landed at 20:18:51 as
249 s into the wait against 331 s of wall time, and the wait ended with all 20
shards.
