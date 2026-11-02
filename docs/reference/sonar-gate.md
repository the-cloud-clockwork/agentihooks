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

The merge produces `coverage.xml`, the coverage report table and `lcov.info`.
Python XML still covers `hooks` and `scripts`; JavaScript LCOV still includes
node and browser captures. The optimization changes coverage preparation only:
Sonar sensors, rules, scan inputs and Delivery L2 conditions remain enabled.

For full scope runs, `Build the test dependency manifest` independently combines
the original per test contexts with `--keep` into
`$RUNNER_TEMP/dep-manifest/.coverage`, outside the scanned checkout. The manifest
builder needs those contexts to associate tests with code. Using the flattened
scan database for that build would lose those associations.

`Merge shard coverage` runs in the background during proxy and cache setup.
`Wait for the coverage merge` must succeed before `SonarQube Scan` starts. The
manifest build also runs in the background, starts before the cache restores,
and is checked after the scan. Moving it off the scan's prerequisite path does
not make its success optional. Both waits have five minute timeouts.

### Workflow configuration

This behavior is automatic in Tests; it introduces no user setting or secret.
The workflow supplies the merge inputs:

| Input | Current meaning |
|---|---|
| `SHARDS` | Full scope uses 20; selected scope uses the selection job's shard count. Every numbered primary shard database must be present and nonempty. |
| `SCOPE` | `selected` passes `--selected` to the JavaScript LCOV builder. Scope `none` skips the shard download and merge; only `full` builds the dependency manifest. |
| Download layout | `.coverage-shards/coverage-3.12-<shard>/.coverage`; each shard's optional `reused-coverage/.coverage` is included when nonempty. |
| Coverage configuration | `.github/coverage/coverage.ini` enables branch coverage and relative filenames and includes `hooks/*` and `scripts/*` in reports. |
| Scanner report settings | `sonar.python.coverage.reportPaths=coverage.xml` and `sonar.javascript.lcov.reportPaths=lcov.info`. |

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
| `Missing coverage for shard N` | Compare the expected count with the run's downloaded primary shard artifacts; an optional reused report does not replace the required primary database. |
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
