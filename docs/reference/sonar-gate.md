# Sonar quality gate

The `sonar` job in the Tests workflow is a pull request requirement of `Gate — Required`. It scans the current pull request head and fails when the SonarQube quality gate fails. The merge queue runs the unit suite and test count checks; it does not repeat Sonar or the other pull request checks. Promotion owns the slower integration checks, and a red promotion holds release while `dev` keeps merging. See [Delivery tiers](/docs/reference/delivery-tiers/) for the three tiers.

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
whole job duration are separate measurements. A skipped or superseded analysis
supplies no first sensor sample.

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
