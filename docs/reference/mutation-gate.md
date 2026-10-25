---
title: Pull request mutation gate
parent: Reference
nav_order: 13
---

# Pull request mutation gate

The Mutation workflow runs beside Tests for pull requests into dev. It discovers changed Python files in `hooks` and `scripts` from the pull request merge base. Each file runs in an isolated copy with pinned mutmut 3.6.0. Pytest runs serially; matching, importing and recorded covering tests provide mutmut's function coverage stats and per mutant test selection.

Run a focused mutation proof in CI from a pushed branch:

```bash
gh workflow run proofs.yml \
  --ref YOUR_BRANCH \
  -f proof=mutation \
  -f base=origin/dev
```

For a stacked branch, set `base` to its dependency branch. Branch pushes run Mutation preflight before the first draft pull request; wait for its current head to pass. Once a branch has an open pull request into `dev`, the push preflight skips it and the pull request checks own mutation. Tests and mutation runs execute in CI only.

## Covering tests through production helpers

Mutation planning supplements matching and importing test modules with recorded
dev coverage. A test can reach a changed function through production helpers
without importing the mutated module itself. The planner maps graded lines to
their containing functions or methods, then adds existing `test_*.py` files
whose recorded test contexts executed those units. Coverage of another function
in the same module alone does not qualify.

For example, a Doctor test imports `scripts.doctor.loop`, which calls
`VerdictStore.visible` in `scripts.swarm.health.verdicts`. When that method
changes, recorded execution connects the test to it despite the indirect
import. No extra import or manual call chain configuration is needed. Recorded
execution supplies candidates; it does not prove a test asserts enough to kill
a mutant.

The plan saves the selection by changed source path in
`.mutation-selection.json`, uploaded as `mutation-selection`. Statistics jobs
consume this list, and measured refinement uses the same saved list instead of
repeating import selection. This keeps helper callers in the measured plan and
keeps its statistics keys consistent with the collected parts. If the saved
file is absent, the refine command falls back to import selection.

### Usage and configuration

The branch preflight and pull request Tests workflow pass the repository to
the planner automatically. Their planning command is:

```bash
python -m scripts.ci_mutation.plan \
  --base "$BASE" \
  --repository "$GITHUB_REPOSITORY"
```

This runs inside CI with dependencies installed and `GH_TOKEN` supplied by the
workflow. Use the pushed branch preflight or the focused CI proof above; do not
run tests or mutation locally. `--base` defaults to `origin/dev` and `--head`
to `HEAD`. `--repository OWNER/REPO` enables lookup of a usable dev test
dependency record available before the lookup's UTC cutoff. Omitting it uses
import selection only. There is no environment switch for the helper coverage
rule.

Current planning can narrow candidate files to recorded test node ids. See
[Individual tests for statistics](#individual-tests-for-statistics) for the
line coverage rule, compatibility with older function records and whole file
fallbacks. The saved list preserves helper callers and can mix file paths and
node ids; inspect it instead of expecting a fixed test count.

### Inspect the selection

Replace `RUN_ID` and `EVIDENCE_DIR` with the run and a task evidence folder:

```bash
gh run view RUN_ID --log

gh run download RUN_ID \
  --name mutation-selection \
  --dir EVIDENCE_DIR/selection

jq '."scripts/swarm/health/verdicts.py"' \
  EVIDENCE_DIR/selection/.mutation-selection.json
```

A successful lookup prints
`Recorded coverage from dev run RUN_ID adds the tests that executed a changed function.`
Read the returned entries for the expected helper caller, then inspect the
mutation report to establish whether those tests killed the mutant. A reused
report can skip planning and have no new selection artifact; follow its
`reuse.json` to the source run. The selection artifact is retained for one day;
mutation reports are retained for seven days.

### Selection limits and troubleshooting

| Symptom | Meaning and action |
|---|---|
| `No dev test dependency manifest found; tests are selected by import only.` | Lookup completed without a usable record. Import selection still applies; inspect dev Tests record production and lookup diagnostics before assuming indirect callers were included |
| `Mutation selection fallback` warning | Current code retries a coverage read failure once, then selects every test module for each changed file. Read the preceding attempt errors and repair the record or GitHub access failure; expect more statistics work. See [Unreadable coverage record](#unreadable-coverage-record) |
| Expected helper test is absent | Check the recorded dev run, containing function identity and test file's continued existence. A new call path or renamed function may have no historical execution record; import selection remains available but recorded coverage cannot invent that execution |
| File reports `not mutated, no matching or importing test modules` | Neither selection route supplied a test for a changed file containing functions. Planning fails; supply covering tests and inspect their recorded coverage |
| Candidate file appears as individual node ids | Current drift checks allowed narrowing. Compare those ids with the function's recorded coverage instead of requiring the whole file |
| Selected test still leaves a survivor | Execution coverage does not ensure an assertion detects the change. Read the mutant diff and strengthen the relevant assertion, or obtain an independent equivalent-mutant ruling |

### Delivery evidence

[Pull request 4553](https://github.com/the-cloud-clockwork/agentihooks/pull/4553)
delivered recorded coverage selection and saved-selection refinement on
2026-10-18. Its [source issue](https://github.com/the-cloud-clockwork/agentihooks/issues/4552)
records the original omission:

| Evidence | Result |
|---|---|
| [Original preflight 39117411178](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39117411178), head `352490e` | Three import-selected files; 27 survivors in `VerdictStore.judge` (7) and `VerdictStore.visible` (20) |
| Dev dependency record from [Tests 39138888661](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39138888661) | 101 files recorded execution of those functions, including `tests/doctor/test_loop.py` and `tests/doctor/test_doctor_cli.py`, reaching them through production loop and CLI helpers |
| [Red preflight 39140647781](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39140647781) | The helper-coverage and saved-refinement regression tests failed; two failed and four passed |
| [Delivered Tests 39144942050](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39144942050) | Mutation and Gate Required passed on delivered head `96ef387` |

The source investigation also recorded 38 verdict mutants killed and zero
surviving once the loop test was selected on corrected head `b5ef273` in
[run 39117969221, attempt 2](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39117969221/attempts/2).
That head also removed a fallback, so this supports the selection diagnosis but
is not an identical-head replay proving all 27 original mutants killed.
The helper regression fixture and saved-selection refinement regression are
maintained in `tests/test_ci_mutation_planned_selection.py`.

## Unreadable coverage record

When the planner cannot read the recorded dev coverage, it plans with every
test module rather than failing the planning job. A GitHub or artifact hiccup
then costs extra statistics work, never a failed pull request check. The
behavior lives in `read_record` in `scripts/ci_mutation/plan.py` and applies
wherever the planner runs with `--repository`: the `plan` job of the mutation
preflight and the `mutation-plan` job of the Tests run, both in the step
**Size mutation shards from stored timings**.

### Retry and fallback

The planner reads the record through `find_record` in `scripts/ci_test_plan.py`,
which lists completed dev push Tests runs, lists each candidate's artifacts,
reads its unit shard jobs and downloads its `dep-manifest` zip, all through
`gh api`. One read can end three ways:

| Read outcome | Planner behavior | Log |
|---|---|---|
| A usable record | Import selection plus the tests whose recorded coverage reaches the change, narrowed as described in [Individual tests for statistics](#individual-tests-for-statistics) | `Recorded coverage from dev run RUN_ID adds the tests that executed a changed function.` |
| No usable record, every request answered | Import selection only. No retry; a missing record is an answer, not a failure | `No dev test dependency manifest found; tests are selected by import only.` |
| The read raised a read error | Wait `RECORD_RETRY_SECONDS` (10), read again with the same cutoff time. A second success plans normally; a second failure falls back | `Reading the recorded dev coverage failed on attempt N: REASON` per failed attempt |

A read error is any exception in `RECORD_ERRORS`: a failed or timed out `gh api`
call (`subprocess.SubprocessError`), `OSError`, `ValueError` (unparsable JSON and
an unusable manifest zip or schema included), `KeyError` and
`zipfile.BadZipFile`. Any other exception still fails the step.

After the second failure the planner prints one warning annotation, which
appears on the run summary:

```text
::warning title=Mutation selection fallback::The recorded dev coverage could not be read, so every test module is selected for each changed file: CalledProcessError: gh: Server Error (HTTP 502)
```

`REASON` is the exception class and the `gh` stderr text, decoded with
replacement characters for undecodable bytes; without stderr it is the
exception message. `find_record` wraps a corrupt `dep-manifest` zip, a missing
`manifest.json` or an invalid schema as `UnusableRecord`, so those reasons read
`UnusableRecord: Dev run RUN_ID: File is not a zip file` or
`UnusableRecord: Dev run RUN_ID: "There is no item named 'manifest.json' in the archive"`.
The planner keeps the reason from the second attempt.

In the fallback, every changed source file gets every `test_*.py` file under
`tests/`, at any depth, as its selection. No recorded coverage is consulted and
no narrowing runs, so the selection is a superset of what a readable record
would have produced. The plan still validates the selection, writes
`.mutation-selection.json`, sizes shards and statistics parts from it and
continues. Statistics then collect the whole suite for those files, so expect
more statistics parts and a longer statistics phase on that run.

### Configuration

The fallback has no switch. The planner command is the existing CI step:

```bash
python -m scripts.ci_mutation.plan --base "$BASE" --repository "$GITHUB_REPOSITORY"
```

The step needs `GH_TOKEN` for `gh api`. The retry delay is the
`RECORD_RETRY_SECONDS` constant, one retry is fixed, and each `gh api` call
times out after 60 seconds. Without `--repository` the planner reads no record
and uses import selection.

### Inspect a run

Find the planner step's lines in the job log:

```bash
gh run view RUN_ID --log | grep -E "Reading the recorded dev coverage failed|Mutation selection fallback|Recorded coverage from dev run|No dev test dependency manifest"
```

A recovered read shows one `attempt 1` line followed by `Recorded coverage from
dev run`. A fallback shows both attempt lines and the warning. Confirm the
selection with the planner artifact, downloaded as in
[Inspect the selection](#inspect-the-selection):

```bash
jq -r '.[] | length' EVIDENCE_DIR/selection/.mutation-selection.json | sort -u
```

In a fallback every changed file lists the same count, the number of test
modules in the tree.

### Limits and troubleshooting

| Symptom | Meaning and action |
|---|---|
| `Mutation selection fallback` warning on one run | The record read failed twice. Read the reason; a transient `HTTP 5xx` or timeout needs nothing, and the next run reads the record again |
| The same warning on every run | The failure is persistent. A `gh` authentication or permission error points at `GH_TOKEN`; a reason starting `UnusableRecord: Dev run RUN_ID:` (raised by `_usable` in `scripts/ci_test_plan.py`) names a dev run whose `dep-manifest` zip or schema is unusable, which the planner reads again on every attempt; repair the record production on dev |
| Statistics take much longer than usual | Expected after a fallback: the whole suite runs for each changed file. Check the warning before diagnosing the statistics shards |
| Planning step fails with a traceback | The exception is outside `RECORD_ERRORS`, or the failure is elsewhere in planning; read the traceback |
| `attempt 1` line followed by a normal plan | The retry recovered; the selection is the coverage based one |

The fallback covers reading the record only. A failed upload or download of
the planner's own selection artifact between jobs is a separate transfer
failure; see [Reused and empty pull request runs](#reused-and-empty-pull-request-runs).

### Delivery evidence

[Pull request 5201](https://github.com/the-cloud-clockwork/agentihooks/pull/5201)
delivered the retry and fallback on 2026-10-20 for
[issue 5199](https://github.com/the-cloud-clockwork/agentihooks/issues/5199).

| Evidence | Result |
|---|---|
| [Mutation preflight 39263117668](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39263117668), 2026-10-19 | Planning failed downloading the `dep-manifest` artifact zip with a `gh api` `CalledProcessError` |
| [Mutation preflight 39227109213](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39227109213), 2026-10-19 | Planning failed listing dev runs with a `gh api` `CalledProcessError` |
| [Red Proofs 39399678910](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39399678910) | Tests only: 6 failed, 50 passed on the old planner |
| [Green Proofs 39400480288](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39400480288) | 149 passed across the planned selection, plan and preflight test modules on the delivered head |

The source investigation found the artifact unexpired and downloadable
afterwards, and the run listing reads no artifact. Nothing observed showed that
the organization's private artifact storage quota, reported over its included
amount the same day, caused either failure; the old planner discarded the `gh`
stderr, which the fallback reason now carries. The regression cases are
`test_plan_retries_an_unreadable_record_once_then_selects_every_test` and
`test_record_failure_names_the_gh_error_text` in
`tests/test_ci_mutation_planned_selection.py`: one failure then a recovered
read keeps the coverage selection, and two failures from a 502, a timeout, a
bad zip, unparsable JSON or a missing manifest select every test module.

## Individual tests for statistics

Statistics collect the tests that reach the changed code instead of every test
in each candidate file. Recorded dev coverage still brings in callers through
production helpers. Narrowing then chooses test function or method node ids
within those files, so unrelated tests in a large file need not run during
statistics preparation.

With a manifest containing per-test `lines`, the planner selects tests whose
recorded executed source lines intersect the graded lines. It maps current
lines back to recorded lines, preserving coverage when earlier edits shift
line numbers. Equal-length replacements map to their old lines; changed lines
elsewhere since the record are considered too. A test reaching only another
line in the same function can be excluded when this mapping is available.
An older manifest without `lines` uses recorded function or method units
instead: every test reaching a changed function remains eligible.

For example, two tests in one file call different branches of `visible`.
Changing a line executed only by `test_visible_pending` can produce this saved
selection when line contexts are complete and drift permits narrowing:

```json
{
  "scripts/swarm/health/verdicts.py": [
    "tests/test_verdicts.py::test_visible_pending",
    "tests/doctor/test_loop.py::TestLoop::test_visible_verdict",
    "tests/test_legacy_verdicts.py"
  ]
}
```

These names illustrate the format. The second entry represents an indirect
helper caller; the last represents a file retained because its contexts are
incomplete. Selection is at test function or method granularity: parameter
suffixes are removed for selection, keeping the function's parameter cases.
Consumers also accept recorded ids carrying xdist `@group` suffixes. Statistics
partitioning, duration estimates, per-path filtering and cache identity accept
the mixed list.

### Conservative fallbacks

Missing evidence broadens selection. It never authorizes discarding a covering
test merely to meet a time target.

| Record or intervening change | Selection behavior |
|---|---|
| Candidate test file changed since the manifest, a defined test has no recorded id, a test has no recorded units, or a line manifest lacks that test's line context | Keep that whole candidate test file |
| A changed function has no recorded unit | Keep candidate files whole for that source path |
| Manifest has no commit, a shared test helper or conftest changed, dependency inputs or the unit test workflow changed, or a Python module outside the measured roots changed | Disable narrowing; retain candidate files |
| Inserted or deleted source cannot map directly | Include tests reaching the affected recorded functions; added or removed modules use their recorded units |
| Import-time source changes or replaced lines outside a function | Include tests reaching recorded units in that module |
| Changed data file was opened by a recorded test | Retain that test as a covering candidate |
| Narrowing would leave a source path with no tests | Keep its original candidate list |

Escape-only tests do not supply recorded mutant coverage: their child process
uses a light bootstrap that does not record trampoline hits. They cannot
substitute for a test that executes the instrumented function. Missing contexts
still trigger the conservative file fallback above.

### Configuration and inspection

The existing CI planner command with `--repository "$GITHUB_REPOSITORY"`
enables recorded selection automatically. There is no separate environment
switch or time threshold for choosing individual tests. The manifest's
available coverage and intervening changes determine the granularity. Without
a usable dev manifest, selection uses matching and importing modules; a
coverage lookup failure follows the broader fallback described above.

Use the [selection download example](#inspect-the-selection) to inspect one
source path, or list every source and selected entry:

```bash
jq -r 'to_entries[] | .key as $source | .value[] | [$source, .] | @tsv' \
  EVIDENCE_DIR/selection/.mutation-selection.json
```

A `::` entry selects a test function or method; a bare `.py` entry selects a
whole file. Check the statistics job and mutation report as well: fewer entries
alone do not establish equal mutant grading or a shorter job wall time.

| Symptom | Check |
|---|---|
| A large test file remains whole | Compare it with the manifest commit, check recorded test ids and contexts, and inspect shared helper or dependency drift |
| Many ids from the same function remain | Check whether the manifest has `lines`; older records select by function. Current records also broaden when line mapping is unsafe |
| A helper caller disappears | Check that its recorded context executes the changed line or fallback unit and the test file still exists; preserve the helper inclusion rule |
| Statistics time differs from the sum of selected durations | Collection, instrumentation, setup, runner contention and cache reuse also affect wall time; compare the same source, record and duration corpus |

### Line contexts in the dependency manifest

The line rule depends on the dev dependency manifest that the full-scope Tests
run records. Python 3.12 unit shards run with `--cov-context=test`; the
`Build the test dependency manifest` step combines their coverage and runs
`python -m tests.dep_manifest build`. The `dep-manifest` artifact holds
`manifest.json`, which is also cached under `dep-manifest-<sha>` when Gate
Required passes. Mutation planning reads that record through the
`--repository` lookup described above.

| Manifest field | Content |
|---|---|
| `version` | `2` |
| `commit` | The dev commit the coverage was recorded on; drift checks compare the pull request against it |
| `units` | Every recorded function, method or module unit, as `path::Qualified.name` or `path::<module>` |
| `tests` | Per test: `units` (indexes into `units`), data `files` it opened, and `escapes` for escape-only child processes |
| `lines` | Per test: each measured source path and the sorted line numbers that test executed |

Test ids come from the coverage context with its phase suffix after the last
`|` removed, so setup, call and teardown lines of one test merge into one
entry. A test executes a line when coverage recorded that line inside the
test's context; a line reached only during an import in another test's context
belongs to that other test. A `tests` entry with no `lines` entry is selected
whenever planning considers it, never dropped.

Download the record a dev run produced and count the tests that execute each
line of one function. Replace `DEV_RUN_ID` and `EVIDENCE_DIR` with the dev run
and a task evidence folder:

```bash
gh run download DEV_RUN_ID --name dep-manifest --dir EVIDENCE_DIR/manifest

jq -r '[.version, .commit, (.tests | length), (.lines | length)] | @tsv' \
  EVIDENCE_DIR/manifest/manifest.json

jq -r --arg path scripts/swarm/health/verdicts.py \
  '[.lines[] | .[$path] // [] | .[]] | group_by(.) | map("\(.[0]) \(length)") | .[]' \
  EVIDENCE_DIR/manifest/manifest.json
```

List the tests that execute one line, and the tests that have no line context
and therefore keep their whole file:

```bash
jq -r --arg path scripts/swarm/health/verdicts.py --argjson line 27 \
  '.lines | to_entries[] | select(.value[$path] // [] | index($line)) | .key' \
  EVIDENCE_DIR/manifest/manifest.json

jq -r '[.tests | keys[]] - [.lines | keys[]] | .[]' \
  EVIDENCE_DIR/manifest/manifest.json
```

Dev run [39910141570](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39910141570)
on `cbf887e` recorded 42,974 tests, 42,972 of them with line contexts. In
`VerdictStore.visible`, 935 tests executed the method's first body line, 194
reached the loop body, and one,
`tests/doctor/test_loop.py::test_a_legacy_verdict_adopts_evidence_identity_without_returning_on_new_wording`,
executed line 27. Under the function rule a change to line 27 selects all 935
tests; under the line rule it selects that one test, plus any helper caller,
fallback unit or whole file the conservative rules keep. The two tests without
line contexts were Postgres ledger tests and keep their whole files.

| Symptom | Check |
|---|---|
| `jq` reports `null` for `lines` | The record predates line contexts; selection falls back to function units for every test |
| A test listed for the line is missing from the selection | Check that its file still exists and is named `test_*.py`; recorded files that are gone or renamed are not added as candidates |
| A test that never reaches the changed line is selected | It has no line context, it opened a changed data file, or the line falls in an insertion, deletion, import-time change or replaced block the mapping cannot place |
| The line numbers in `lines` differ from the pull request's | The record is from `commit`; planning maps the pull request's lines back through the diff from that commit before comparing |

### Paired selection proof

The `Mutation line selection proof` workflow
(`mutation-line-selection-proof.yml`) grades one sample change twice to show
the narrower selection gives the same verdicts. It checks out the
[pull request 4473](https://github.com/the-cloud-clockwork/agentihooks/pull/4473)
merge commit `328ddd3` as the sample, copies the dispatched branch's
`scripts/ci_mutation`, `tests/dep_manifest.py` and
`tests/mutation_line_selection_proof.py` over it, and runs
`python -m tests.mutation_line_selection_proof` in four modes:

| Mode | Result |
|---|---|
| `prepare` | Runs the tests import selection picks for the sample's changes with test contexts, builds a manifest, and writes `selection.json` with the function-rule (`before`, the manifest without `lines`) and line-rule (`after`) selections and their stored test seconds. Fails unless `after` costs fewer seconds |
| `before` | Mutates `scripts/swarm/health/verdicts.py` against the `before` selection and writes `before-verdicts.json` |
| `after` | The same against the `after` selection; writes `after-verdicts.json` |
| `verify` | Fails on any mutant whose verdict differs and prints `Identical verdicts for N sample pull request mutants` |

Dispatch it on a branch that carries a selection change and read the result.
Replace `BRANCH` with the dispatched branch and, after dispatch, `RUN_ID` and
`EVIDENCE_DIR` with the run and a task evidence folder:

```bash
gh workflow run mutation-line-selection-proof.yml --ref BRANCH

gh run download RUN_ID --name mutation-line-selection-proof --dir EVIDENCE_DIR/line-proof

jq '{before: .before.seconds, after: .after.seconds,
     before_entries: (.before.tests | length), after_entries: (.after.tests | length)}' \
  EVIDENCE_DIR/line-proof/selection.json
```

The artifact also holds both verdict files and each grading `run.log`, and is
retained for seven days. Both sides share the sample's source, tests, contexts
and durations, so the comparison isolates the selection rule; it does not
measure statistics job wall time on live pull requests.

### Delivery and measurement limits

[Pull request 4710](https://github.com/the-cloud-clockwork/agentihooks/pull/4710)
delivered individual test ids using function coverage. Its reported red
[Tests run 39216973077](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39216973077)
selected whole files in the narrowing regression. Its green
[Tests run 39221081659](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39221081659)
passed selected units, statistics, three mutation shards and the gate. The
recorded verdict-store replay selected 562 seconds before and 422.5 seconds
after; 788 recorded tests reached `VerdictStore.visible`. The source request's
528 seconds and approximately 26-second covering share were investigation
figures, not the delivered replay's result.

The later [pull request 4809](https://github.com/the-cloud-clockwork/agentihooks/pull/4809)
delivered the current line coverage selection requested in
[issue 4792](https://github.com/the-cloud-clockwork/agentihooks/issues/4792).
Red Proofs runs
[39251990627](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39251990627)
(recording and selection regressions) and
[39253762512](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39253762512)
(fallback regressions) failed on the old selection; green Proofs run
[39258209332](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39258209332)
passed the 86 regression checks. Its
[paired proof 39258204668](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39258204668)
reported 530 entries / 373.63 stored test seconds before and 115 entries /
20.33 seconds after, with all 38 mutation verdicts equal. Both sides used the
same source, tests, contexts and durations. This is a different recorded corpus
from the earlier replay; the two reductions must not be combined. The later
[green Tests run 39260211042](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39260211042)
graded all 187 planned mutants, with 183 killed, four independently cleared and
zero failures. These proofs establish selection behavior and sample grading;
they do not establish the requested ten-before versus ten-after statistics
job wall-time comparison.

## Continuation publication and preflight runs

When a successor continues an existing pull request, `swarm publish` and the
Stop push gate select the retained pull request branch when its tracking ref
is an ancestor of the new head. They do not additionally push the successor's
prepared branch. One publication therefore produces one branch push, avoiding
two Mutation preflight runs caused by publishing the same commit on two
branches. See [continuation usage, configuration and failures](../pillars/swarm.md#continue-an-existing-pull-request-with-one-push).

Inspect the runs for the published commit across all branches, rather than
filtering only to the pull request branch. Run from the repository checkout
and replace `PUBLISHED_SHA` with the `head` returned by `swarm publish`:

```bash
gh api --method GET \
  'repos/{owner}/{repo}/actions/workflows/mutation-preflight.yml/runs' \
  -f event=push \
  -f head_sha=PUBLISHED_SHA \
  -f per_page=100 \
  --jq '.workflow_runs[] | {id, head_branch, head_sha, status, conclusion, html_url}'
```

For a single continuation publication without independent pushes or retries,
expect one run on the retained branch and none on the prepared successor
branch. Inspect that run with `gh run view RUN_ID --log`. An open pull request
into `dev` makes the push preflight skip mutation; its pull request checks own
the mutation work. A workflow record alone does not prove that mutants ran,
and an empty run list does not establish success. Confirm the workflow has
started and the returned head matches before recording the count.

If the same head appears on two branches, compare both `head_branch` values
with the publication result and continuation record. Check for a separate
manual push of the prepared branch. If ancestry no longer holds after
restacking, a new prepared branch can be the intended publication target.
Branch selection does not disable preflight or change its mutation grading
and report reuse rules.

## Touched test worker sizing

The branch preflight's `touched-tests` job runs the existing test files changed
between `origin/dev` and `HEAD`. It first collects their test cases without
starting xdist workers. Parametrized cases count separately. The worker limit
is `max(1, min(automatic maximum, collected cases))`:

| Collected cases | Automatic maximum | Execution workers |
|---|---|---|
| 4 | 16 | 4 |
| 10 | 2 | 2 |
| 1 | 16 | 1 |

This avoids starting a full worker pool for a small regression selection. It
does not change which tests run or how full unit shards choose their workers.
The current touched run uses `--dist loadgroup`: a group can still keep several
cases on one worker, so worker count is an upper bound on concurrency.

### Automatic configuration

No new flag enables the limit. `worker_count` in `scripts.ci_touched_tests`
collects with `--collect-only -q -n 0` and asks the pytest
`pytest_xdist_auto_num_workers` hook for the existing automatic maximum. This
preserves custom worker hooks and the default CPU detection. The workflow
writes the result to its `workers` step output and supplies it only to
**Run the tests the branch touches**:

{% raw %}
```yaml
env:
  FILES: ${{ steps.touched.outputs.files }}
  PYTEST_XDIST_AUTO_NUM_WORKERS: ${{ steps.touched.outputs.workers }}
  PYTHONUNBUFFERED: "1"
run: |
  read -ra files <<< "$FILES"
  python -m pytest "${files[@]}" -n auto --dist loadgroup --tb=short -p no:cacheprovider
```
{% endraw %}

This is existing CI configuration. Run tests in CI, not on the shared host.
Keep the computed value scoped to the execution step; a job wide value would
also affect the maximum read during collection. Change the existing worker
policy through a reviewed code change when its maximum needs adjustment.

If no existing test files changed, selection outputs `workers=1` but skips
execution. If collection exits nonzero, selection exits with that status and
the job fails before execution workers start. The one worker floor does not
turn an empty or failed collection into a passing run.

### Inspect a branch run

Push the branch before opening its pull request, then inspect its preflight:

```bash
gh run list --workflow mutation-preflight.yml --branch <branch> --limit 5
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run-id>/jobs \
  --jq '.jobs[] | {id, name, conclusion}'
gh run view <run-id> --job <touched-tests-job-id> --log
```

In **Select the tests the branch touches**, check the collected case count and
the `Touched tests:` file list. In **Run the tests the branch touches**, check
`PYTEST_XDIST_AUTO_NUM_WORKERS` and xdist's `created: N/N workers` line. Match
the run's head revision before comparing results. An open pull request makes
the branch preflight skip; a skipped run provides no startup sample.

| Symptom | Check |
|---|---|
| Selection fails before workers start | Read the collection exception; fix the test import, syntax or collection configuration. |
| More cases than changed files | Parametrization and multiple tests per file increase the collected count. |
| Fewer workers than cases | The existing automatic maximum can be lower than the selection size. |
| Several cases run on one worker | `loadgroup` scheduling preserves test groups. |
| Execution is skipped | Check for existing changed test files, a collection failure or an open pull request. |

### Delivery evidence and timing limits

[Pull request 4717](https://github.com/the-cloud-clockwork/agentihooks/pull/4717)
merged on 19 October 2026. Its
[red proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39214635133)
exposed excess workers for four and one cases and workers starting before a
collection error. The
[green preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39217754530)
passed regressions for small selections, the maximum, the floor, default CPU
detection, custom worker hooks and collection failure; its mutation grade
killed all 26 mutants.

The delivery reports ten earlier runs starting sixteen workers, with median
startup of 4.65 seconds. Historical runner CPU seconds were not recorded and
the ten run after comparison remained unverified. These proofs establish
worker sizing, not a measured CPU saving or a guaranteed startup duration.
For a new comparison, retain ten runs on each side with ten or fewer selected
cases, exact revisions, raw job timestamps and the same startup interval.
Record CPU seconds separately when available; whole job duration and runner
queue wait measure different intervals.

## Mutation statistics and shard worker sizing

The `mutation-stats` jobs fork one process per statistics bucket, and each
`mutation` shard passes `--max-children` to mutmut. In CI both counts are now
capped by the runner container's CPU quota, so a batch runner limited to three
CPUs on a sixteen core node starts three buckets or three mutmut children
instead of sixteen.

`worker_count` in `scripts.ci_mutation.selection` takes the CPU count each step
can see and returns:

| Environment | Worker count |
|---|---|
| `CI` set to a non-empty value, finite CPU quota | `max(1, min(CPU count, int(quota)))` |
| `CI` set to a non-empty value, no finite quota | the CPU count |
| `CI` unset or empty | `min(CPU count, 2)` |

The quota comes from `quota_cpus` in `scripts.ci_cpu_quota`, the helper the
unit shards and the Proofs `tests` proof use. It reads every finite cgroup
v2 `cpu.max` or v1 `cpu.cfs_quota_us` and `cpu.cfs_period_us` limit on the
process's cgroup path and its parents and keeps the smallest. A fractional
quota is truncated; a quota below one CPU still gives one worker.

| Step | CPU count read | Further cap |
|---|---|---|
| **Collect one stats part for every mutation shard** (`mutation-stats`) | `os.sched_getaffinity(0)` | `stats_shards` makes at most one bucket per test unit, so a selection of two test files never forks more than two buckets |
| Statistics of a `python -m scripts.ci_mutation` run without `--stats`, collected inline before mutating | `os.sched_getaffinity(0)` | the same `stats_shards` cap |
| **Mutate changed Python files**, **Mutate dispatched Python files** (`mutation`) and **Mutate the only shard in the statistics job** | `os.cpu_count()` | none |

The branch preflight in `mutation-preflight.yml` runs the same steps in its
`stats` and `mutation-shard` jobs. `stats_shards` keeps files that share an `xdist_group` in one
bucket, so the number of buckets can be lower than the worker count. A shard
given `--stats` reuses the collected parts and forks no buckets.

For example, on a runner with sixteen visible CPUs and a `cpu.max` of
`300000 100000`:

| Selected test units | Statistics buckets | mutmut `--max-children` |
|---|---|---|
| 40 | 3 | 3 |
| 2 | 2 | 3 |

With a `cpu.max` of `250000 100000` both values for 40 units are 2; with
`50000 100000` they are 1.

### Configuration

There is no input, flag or environment variable for this worker count. Change
it by changing the runner's CPU limit or CPU affinity; a CPU request without a
limit sets no finite cgroup quota, so the step uses every visible CPU. The cap
does not limit processes a test starts itself.

Local runs, where `CI` is unset, keep the two worker limit from the local
mutation run. Run the mutation gate in CI, not on the shared host.

### Inspect a run

The selection does not print its worker count. Read the runner's quota and the
job wall times instead:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/<run-id>/attempts/1/jobs?per_page=100" \
  --jq '.jobs[] | select((.name|startswith("mutation-stats")) or (.name|startswith("mutation ("))) | [.name, .runner_name, .started_at, .completed_at] | @tsv'
```

To see the quota a runner image exposes, run the same helper the unit shards
log, in a CI step on that runner label:

```bash
python -m scripts.ci_cpu_quota
```

It prints `pytest workers N: cpu quota Q, affinity A` on standard error and the
count on standard output. `cpu quota None` means no finite limit was found, so
the mutation steps use every visible CPU.

| Symptom | Check |
|---|---|
| Statistics or shards start as many workers as the node has cores | The runner has no finite CPU limit; `cpu quota None` in the helper output confirms it. |
| Fewer statistics buckets than the quota | The selection has fewer test units, or several files share an `xdist_group`. |
| One worker on a multi-CPU runner | The quota is below two CPUs, or `CI` is unset or empty and the CPU count is one. |
| Two workers in a local run | Expected: local runs cap at two. |

### Worker sizing delivery evidence

[Pull request 4979](https://github.com/the-cloud-clockwork/agentihooks/pull/4979)
merged on 20 October 2026 at 02:48 UTC. Its
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39343122438)
failed the new quota cases with `--max-children 16`, and the
[green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39344403807)
passed `mutation-plan`, `mutation-stats` and `mutation`. The regression
`test_local_selection_caps_mutation_and_stats_workers` covers sixteen CPUs with
quotas of 3, 2.5 and 0.5, a request below the quota, no quota and local runs.

Job wall times were read with the source task's sampler: successful first
attempts of pull request Tests runs, counting only `mutation-stats` jobs whose
**Collect one stats part for every mutation shard** step succeeded and
`mutation` jobs whose **Mutate changed Python files** step succeeded, all on
`anton-oss-batch` runners. Wall time is `completed_at` minus `started_at`, so
runner queue wait is excluded.

| Sample | Jobs started | Statistics jobs | Shard jobs |
|---|---|---|---|
| Before the merge | 19 October 10:26 to 23:11 UTC | 397 jobs, median 199 s, mean 189.9 s, p90 277 s | 263 jobs, median 102 s, mean 170.9 s, p90 399 s |
| After the merge | 21 October 08:00 to 11:03 UTC | 47 jobs, median 129 s, mean 131.6 s, p90 184 s | 39 jobs, median 121 s, mean 114.0 s, p90 177 s |

The samples are unequal in size and window, the after read was cut short by
GitHub API errors, and other mutation changes merged between the two windows,
so the drop cannot be attributed to worker sizing alone. To repeat the
comparison, sample equal counts of at least ten runs on each side and keep the
selected test counts comparable:

```bash
gh api -X GET repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs \
  -f event=pull_request -f per_page=100 -f created='<start>..<end>' --paginate \
  --jq '.workflow_runs[].id' | head -n 40
```

Then read each run's `attempts/1/jobs` with the job query under
[Inspect a run](#inspect-a-run).

## Shard planning from the batch runner CPU quota

The mutation planner sizes shards and statistics parts for the number of test
workers a batch runner starts, which is three under the batch runner's CPU
limit of three. Before this change it assumed four, so its estimates were
optimistic by a third and a shard could run past its budget.

Most planning steps run on the light runner, whose own CPU limit is two, so
they cannot read the batch quota from their cgroup. The one exception is the
**Refine mutation shards from measured statistics** step inside the `stats`
job, which runs only when a single job covers the whole plan and runs on the
batch runner itself; it reads the variable like the others. Each planning step
passes the batch runner's quota in `MUTATION_SHARD_CPU_QUOTA`, and
`shard_workers` in `scripts.ci_mutation.plan` turns it into a worker count
with `worker_count` from `scripts.ci_cpu_quota`, the same function the unit
shards and the Proofs `tests` proof use to choose their pytest workers.

| `MUTATION_SHARD_CPU_QUOTA` | Planned workers |
|---|---|
| unset or empty | 4 |
| `3` | 3 |
| `4` or higher | 4 |
| `2.5` | 2 (a fractional quota is truncated) |
| `0.5` | 1 (never below one) |
| not a number | the planning step fails with a `ValueError` |

The rule is `max(1, min(4, int(quota)))`, with four as the CPU count, so the
variable can lower the planned workers but never raise them above four.

### What the worker count sizes

| Planner call | Step | Effect of fewer workers |
|---|---|---|
| `shard_count` | **Size mutation shards from stored timings** (`plan`, target 120 s) and **Refine mutation shards from measured statistics** (`refine`, target 240 s) | Shards = `ceil(worker seconds / (workers * target))`, capped by the shard limit of ten and the mutant count, so more shards are planned for the same work |
| `slowest_bucket` and `stats_part_count` | **Size mutation shards from stored timings** | Each statistics part forks one bucket per worker; the slowest bucket grows, so the step can choose more `Stats parts` to stay within the 60 s statistics target |
| `plan_shards` | **Refine mutation shards from measured statistics** | The measured plan records the count in its `workers` field |

For example, a measured plan of 2880 worker seconds with the 240 second
target gets `ceil(2880 / (3 * 240))` = 4 shards at a quota of 3, and
`ceil(2880 / (4 * 240))` = 3 shards at a quota of 4. Twelve test files of ten
seconds each in one statistics part give a slowest bucket of 40 seconds at
three workers and 30 seconds at four.

### Configuration

Every step that runs a mutation planner sets the variable to the batch
runner's CPU limit:

| Workflow | Step |
|---|---|
| `mutation-preflight.yml` | **Size mutation shards from stored timings** and both **Refine mutation shards from measured statistics** steps |
| `test.yml` | **Size mutation shards from stored timings** in the `mutation-plan` job |
| `mutation-replay.yml` | **Preserve retained scope and refine its mutation plan** |
| `mutation-refinement-proof.yml` | **Baseline refinement planning** and **Optimized refinement planning** |

A new planning step needs the same entry:

```yaml
      - name: Refine mutation shards from measured statistics
        env:
          MUTATION_SHARD_CPU_QUOTA: "3"
        run: python -m scripts.ci_mutation.measured --bases "$BASES" --stats .mutation-stats
```

`test_every_mutation_planning_step_reads_the_batch_runner_cpu_quota` scans
every workflow for steps that run `scripts.ci_mutation.plan`,
`scripts.ci_mutation.measured`, `scripts.ci_mutation.replay prepare` or
`.refinement-proof/run.py`, and fails when any of them lacks
`MUTATION_SHARD_CPU_QUOTA: "3"`. When the batch runner's CPU limit changes,
change the value in every one of those steps and in that test together.

The variable only affects planning. The mutation and statistics jobs still
choose their own workers from the quota they read on the batch runner, as
described under
[Mutation statistics and shard worker sizing](#mutation-statistics-and-shard-worker-sizing).

### Inspect a plan

The plan step prints `Mutation shards: N` and `Stats parts: P`; the `refine`
step prints `Measured mutation worker seconds` and `Measured mutation shards`.
The measured plan carries the worker count it was sized with:

```bash
gh run download RUN_ID --name mutation-plan --dir EVIDENCE_DIR/plan
jq '{workers, shards, estimated_worker_seconds, shard_worker_seconds}' \
  EVIDENCE_DIR/plan/plan.json
```

`workers` reads 3 on a current preflight. To confirm the batch runner's real
quota, run `python -m scripts.ci_cpu_quota` in a CI step on the batch runner
label; it prints `pytest workers N: cpu quota Q, affinity A`.

| Symptom | Check |
|---|---|
| `workers` is 4 in a measured plan | The planning step does not set `MUTATION_SHARD_CPU_QUOTA`, or the head predates pull request 5140. |
| Shards still run out of budget with `workers` at 3 | Compare the batch runner's `cpu quota` with the variable; a lower quota on the runner means the value in the workflows is stale. |
| A planning step fails with `could not convert string to float` | `MUTATION_SHARD_CPU_QUOTA` holds a non-numeric value. |
| The workflow scan test fails | A planning step was added or renamed without `MUTATION_SHARD_CPU_QUOTA: "3"`. |

### Shard planning delivery evidence

[Pull request 5140](https://github.com/the-cloud-clockwork/agentihooks/pull/5140)
merged on 20 October 2026 at 08:41 UTC. Its
[red Proofs run 39376352913](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39376352913)
on the tests-only head planned three shards at a quota of 3, where three
workers need four shards, and failed the workflow scan; the quota 4 case passed. The
[green Proofs run 39376809049](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39376809049)
passed 151 plan, preflight and selection tests. The regression
`test_shards_are_sized_by_the_workers_the_batch_runner_quota_starts` covers
quotas of 3 and 4 across `shard_count`, `slowest_bucket` and `plan_shards`.
No shard wall time comparison was recorded for this change.

## Single shard plans in the statistics job

On a pull request into dev and on a dispatched Tests run, a mutation plan with
one statistics part and one shard mutates that shard inside `mutation-stats (0)`
and skips the separate `mutation` job. The shard no longer waits for a second
batch runner, a second checkout and a second dependency install. Plans with two
or more shards or two or more statistics parts keep their separate `mutation`
shard jobs. Which mutants are graded, the tests that run them and the report
format are unchanged.

### When the shard runs inline

The decision reads two outputs of `mutation-plan`:

| `shards` | `stats_parts` | `forwarded` | Statistics job | `mutation` job |
|---|---|---|---|---|
| `[0]` | `[0]` | not `true` | Collects its part, then runs **Mutate the only shard in the statistics job** | Skipped |
| `[0]` | `[0]` | `true` | Runs no mutation step; the plan job already uploaded `mutation-report-0` | Skipped |
| `[0]` | two or more parts | any | Collects its parts | One shard job, `mutation (0)` |
| two or more shards | any | any | Collects its parts | One job per shard |

`python -m scripts.ci_mutation.plan` writes `shards` and `stats_parts` from its
measured shard and part counts. A reused report also sets both to `[0]`; see
[Reused and empty pull request runs](#reused-and-empty-pull-request-runs).

The inline step runs after **Upload mutation stats part**, on the same runner:

```bash
python -m scripts.ci_mutation --bases "$BASES" --budget 1080 --shard 0 --shards 1 --stats .mutation-stats
```

It is the command a `mutation` shard runs, with the shard fixed at 0 of 1 and
the statistics read from the part the job just collected, so the job downloads
no `mutation-stats-*` artifact. **Upload complete inline mutation evidence**
then uploads `mutation-report-0`, as `mutation (0)` would: only `report.json`
when the job succeeded, the whole `.mutation-gate/` folder otherwise. Both
steps share the statistics job's 26 minute timeout, and the 1080 second budget
is the same as a shard's.

### Gate Required and graded bases

Gate Required sets `INLINE` to `true` when `shards` and `stats_parts` are both
`[0]`. With `INLINE` true it drops `mutation` from the jobs a pull request
requires and accepts that job as skipped; `mutation-plan` and `mutation-stats`
must still succeed. A failed inline step fails `mutation-stats (0)`, so the
gate fails as it would on a failed shard. The inline form came with
[pull request 5180](https://github.com/the-cloud-clockwork/agentihooks/pull/5180):
`inline_mutation` in `scripts/ci_reuse.py` builds the same form, and
`scripts/gate_protected.py` accepts it as the protected gate.

`graded_green` in `scripts.ci_mutation.scope` decides whether a pushed commit
counts as a [graded base](#graded-lines). For an open pull request it reads the
Tests runs of that commit and counts a run as graded when every shard result
in it succeeded. A shard result is a `mutation (<n>)` job's conclusion or,
for a `mutation-stats` job whose inline step ran, that job's conclusion. A head
graded inline therefore stays a base for later pushes, like one graded by a
shard job.

The branch push preflight in `mutation-preflight.yml` has no inline step: it
still runs its `stats` job and then its `mutation-shard` job.

### Inspect a single shard run

List the mutation jobs of a run and the inline step's result:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/<run-id>/attempts/1/jobs?per_page=100" --paginate \
  --jq '.jobs[] | select(.name|startswith("mutation")) | [.name, .conclusion, .started_at, .completed_at, ([.steps[]? | select(.name == "Mutate the only shard in the statistics job") | .conclusion] | join(","))] | @tsv'
```

An inline run shows `mutation-stats (0)` with `success` in the last column and
a `mutation` job `skipped`. On the delivered pull request's green run:

```text
mutation-plan       success  2026-10-20T09:38:58Z  2026-10-20T09:39:55Z
mutation-stats (0)  success  2026-10-20T09:40:01Z  2026-10-20T09:40:50Z  success
mutation            skipped  2026-10-20T09:40:50Z  2026-10-20T09:40:50Z
```

A forwarded run shows the same skipped `mutation` job with the last column
`skipped`. Read the plan outputs from the plan job log, which prints
`Mutation shards: <n>` and `Stats parts: <n>`. The report is read as on any
other run:

```bash
gh run download <run id> -n mutation-report-0 -D mutation-report
jq '{failed, not_mutated}' mutation-report/report.json
```

### Single shard limits and troubleshooting

- The inline step only applies to a plan of one shard and one part. A plan
  that needs two shards pays for the second runner as before.
- The statistics and mutation work share one runner and one timeout, so the
  statistics job of a one shard plan runs longer than before by its mutation
  time.
- The step reuses the statistics job's checkout, dependencies and browser, so a
  setup failure there fails both statistics and mutation in one job.

| Symptom | Check |
|---|---|
| `mutation (0)` ran for a plan that printed one shard | The plan printed more than one `Stats parts`, or the run used a workflow revision older than pull request 5186. |
| `mutation` skipped and no `mutation-report-0` | Read `mutation-plan`: a forwarded run uploads the report there, not in the statistics job. |
| `mutation-stats (0)` failed after its stats part uploaded | The inline step failed; download `mutation-report-0`, which then holds the whole `.mutation-gate/` folder, and read [Clean baseline failures](#clean-baseline-failures) when no mutant was graded. |
| Gate Required fails with `mutation` skipped | The plan had more than one shard or part, or `mutation-stats` did not succeed; the gate accepts a skipped `mutation` only when both outputs are `[0]`. |
| A later push regrades lines of an inline graded head | That head's statistics job did not succeed, or its run was not a pull request Tests run for that commit. |

### Single shard delivery evidence

[Pull request 5186](https://github.com/the-cloud-clockwork/agentihooks/pull/5186)
merged on 20 October 2026 at 10:41 UTC and closed
[issue 5179](https://github.com/the-cloud-clockwork/agentihooks/issues/5179).
Its [red run 39392961654](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39392961654)
on the tests only head failed only the new
`test_a_one_part_one_shard_plan_mutates_inside_the_statistics_job` and
`test_pull_request_grading_counts_the_shard_mutated_inside_the_statistics_job`.
Its [green run 39402043868](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39402043868)
passed, and graded its own one shard plan inline (the run shown above).

The measure is wall time from the `mutation-plan` job start to the end of the
job that graded the shard: `mutation` before the change, `mutation-stats (0)`
after. Both samples are the five newest successful first attempts of pull
request Tests runs whose plan had one part and one shard.

| Sample | Runs created | Plan start to grade | Median | Mean |
|---|---|---|---|---|
| Before, recorded by the source task | 20 October 00:00 to 08:10 UTC | 191, 220, 207, 178, 186 s | 191 s | 196.4 s |
| After, sampled for this page | 20 October 10:42 to 21 October 08:00 UTC | 125, 163, 171, 104, 129 s | 129 s | 138.4 s |

Before runs: 39387418173, 39386682588, 39386638469, 39386392354 and
39381833895, whose separate shard jobs ran 45 to 62 s. After runs:
39555030857, 39554885886, 39543789929, 39530006213 and 39492325418, whose
statistics jobs with the inline shard ran 37 to 68 s. The after sample also
needed its `mutation` job skipped and the inline step successful. Other
mutation changes merged between the two windows, so the 62 s drop in the median
is not attributable to this change alone.

## Planning input reuse

Mutation sizing reuses expensive inputs while building a plan and checking
statistics cache entries. This happens automatically in branch preflight and
the pull request **Size mutation shards from stored timings** step.

| Input | Work retained and reused | Boundary |
|---|---|---|
| Recorded dev coverage | The planner reads a record before estimating changed files and passes the manifest into selection. Job lookup requests 100 jobs per page, reducing pagination on large dev runs | One planner invocation; a read failure can trigger the [coverage retry](#unreadable-coverage-record). More than 100 jobs still require another page |
| Test import index | `mentions` caches Unicode-normalized tokens and relative import detection. `imported_names` caches parsed imports by test text and package. `select_tests` parses a file only when it names a reached module's last component or contains a relative import; its import walk visits statements | In-process reuse by content and package, including repeated selection for changed sources. Each selection still reads test text; unrelated files avoid AST parsing |
| Test duration map | `stats_part_count` computes `file_seconds` once, then passes the same map to every candidate part count and worker bucket. `split_parts` partitions using that map without rereading durations | One statistics part sizing calculation. A new plan reads its current duration input |
| Statistics dependency fingerprints | `StatsInputs.for_test` retains a test file's dependency trace when multiple selectors name that file. `test_inputs` materializes each module's AST walk once while recording its dependencies | One `StatsInputs` instance, separately for measured and unmeasured dependencies. Recorded coverage and dynamically loaded inputs still participate in validation |

These in-memory values reduce repeated planning work. The persisted statistics
cache still validates its inputs before reusing collected results, as described
in [Measured shards and split mutants](#measured-shards-and-split-mutants).
[Report reuse](#reused-and-empty-pull-request-runs) is a separate decision that
can skip planning altogether.

### Planning reuse configuration and usage

There is no enable switch, new cache directory or cache lifetime setting for
this reuse. The workflows supply the repository, base and stored durations.
The existing command, shown here with its default sizing values, runs in CI:

```bash
python -m scripts.ci_mutation.plan \
  --base "$BASE" \
  --head HEAD \
  --repository "$GITHUB_REPOSITORY" \
  --target 120 \
  --stats-target 60 \
  --limit 10
```

`--target` and `--stats-target` are target seconds for mutation shards and
statistics parts; `--limit` caps their counts. They control sizing, not whether
inputs are reused. The planner reads `.test_durations` as JSON when present;
`file_seconds` starts each selected entry at 0.01 seconds and adds matching
recorded test durations. Without the file, that starting estimate remains.
Omitting `--repository` uses import selection without the recorded dev lookup.
The repository lookup needs the workflow's `GH_TOKEN` for `gh api`.

Use the [focused mutation proof](#pull-request-mutation-gate) on a pushed branch
to exercise planning in CI. For a completed run, inspect the log and job step
times without executing tests locally:

```bash
gh run view RUN_ID --log

gh api repos/OWNER/REPO/actions/runs/RUN_ID/jobs --paginate \
  --jq '.jobs[] | {name, steps: [.steps[] | select(.name == "Size mutation shards from stored timings") | {name, started_at, completed_at}]}'
```

Compare equal head, base, recorded manifest and duration inputs when measuring
planning changes. Inspect the [selection artifact](#inspect-the-selection)
to confirm that the same tests remain selected.

### Planning reuse limits and troubleshooting

- A syntax error in an unrelated test module may no longer be found during
  import selection because that file is never parsed. Its own test run still
  fails on the syntax error; a green plan does not validate every test file.
- Missing coverage and unreadable coverage follow the existing
  [record lookup outcomes](#retry-and-fallback). Reuse does not remove the
  retry or the conservative selection after two failed reads.
- A slow first plan can still spend time downloading and parsing the dev
  manifest, reading test files and tracing dependencies before cache lookup.
  Check those phases and the selected test set before attributing the delay
  to mutation execution. Reuse does not guarantee a fixed total step time.

### Planning reuse delivery evidence

[Pull request 5462](https://github.com/the-cloud-clockwork/agentihooks/pull/5462)
closed [issue 5352](https://github.com/the-cloud-clockwork/agentihooks/issues/5352).
Its paired diagnostic used the original 57-mutant change with the same head,
base, recorded manifest and live dev record, alternating before, after, after,
before. The pull request records these observed seconds:

| Phase | Before 1 | Before 2 | After 1 | After 2 |
|---|---:|---:|---:|---:|
| Record lookup | 11.22 | 10.95 | 8.68 | 8.79 |
| Rest of sizing: selection, mutants and durations | 9.43 | 9.34 | 2.51 | 2.26 |
| Statistics fingerprint startup, in the stats job | 18.40 | 17.98 | 14.29 | 13.31 |

Selection matched the uploaded artifact in all four diagnostics. The separate
CI sizing steps were 35 and 29 seconds for the original 57-mutant runs
[39425710887](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39425710887)
and [39426673491](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39426673491),
and 25 and 27 seconds for the delivered branch's larger 145- and 153-mutant runs
[39553508079](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39553508079)
and [39554889987](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39554889987).
Those CI runs used different inputs and are not a paired speed comparison.
The delivery report identified download and parsing of the 13 MB dev record
as most of the remaining sizing step time at that point.

[Red Proofs 39553034360](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39553034360)
observed nine job requests, 120 duration reads, unrelated module parsing and a
duplicate file trace across the four failing seam tests.
[Green Proofs 39553523380](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39553523380)
passed all four, plus the plan, scope, stats cache, selection and test plan
files. These are delivery measurements, not a current runner speed guarantee.

## Plan job dependencies

The `mutation-plan` job of the Tests run installs only the third party
packages that the shard planner and the empty change grade import: mutmut,
pytest, pyyaml and tomlkit, at the specifiers `pyproject.toml` declares. Before
[pull request 5439](https://github.com/the-cloud-clockwork/agentihooks/pull/5439)
the job installed the whole project with `.[dev,all]`, 95 packages, only to
size shards. It now resolves and installs 20 packages in about one second. The
plan outputs, the shard and part counts, the graded bases and the planned test
selection are unchanged. The `mutation-stats`, `mutation` and unit test jobs
still install `.[dev,all]`.

### What the step runs

```yaml
      - name: Install dependencies
        if: steps.reuse.outputs.reused != 'true'
        run: bash "$RUNNER_TEMP/workflow/.github/scripts/pypi-retry.sh" "Install dependencies" -- uv pip install --system "mutmut==3.6.0" "pytest>=8.0" "pyyaml>=6.0" "tomlkit>=0.12"
```

| Package | Specifier | Declared in `pyproject.toml` | Planner module that imports it at module level |
|---|---|---|---|
| `mutmut` | `==3.6.0` | `dev` extra | `scripts.ci_mutation.selection` |
| `pytest` | `>=8.0` | `dev` extra | `scripts.ci_mutation.killers`, `tests.test_collection_imports` |
| `pyyaml` | `>=6.0` | project dependencies | `tests.dep_manifest` (imported as `yaml`) |
| `tomlkit` | `>=0.12` | project dependencies | `scripts.ci_mutation.runner` |

- The packages go into the runner's system Python 3.12. The project itself is
  not installed: **Size mutation shards from stored timings** runs
  `python -m scripts.ci_mutation.plan` and **Grade the empty change set** runs
  `python -m scripts.ci_mutation` from the checkout root.
- The step runs only when **Reuse the branch preflight report graded on this
  head and base** reused nothing. A reused run skips the install and the
  planning, as described under
  [Reused and empty pull request runs](#reused-and-empty-pull-request-runs).
- `pypi-retry.sh` comes from the workflow revision through **Take the helper
  scripts from the workflow revision**. It retries the install only when the
  output names a package index or network outage, waiting 5 s and doubling to
  at most 30 s, until `PYPI_RETRY_SECONDS` (default 150) of backoff has passed.
  Any other failure fails the step at once.

### Adding a planner dependency

`test_mutation_plan_installs_only_what_the_planner_imports_at_the_project_pins`
in `tests/test_ci_workflow.py` keeps the step, the planner and `pyproject.toml`
in agreement. `PLANNER_PACKAGES` in the same file maps each installed
distribution to its import name:

```python
PLANNER_PACKAGES = {"mutmut": "mutmut", "pytest": "pytest", "pyyaml": "yaml", "tomlkit": "tomlkit"}
```

The test makes two checks:

1. The step's `run` line equals the retry wrapper followed by
   `uv pip install --system` and the `pyproject.toml` specifier of every
   `PLANNER_PACKAGES` distribution, quoted and sorted by distribution name.
   Specifiers are looked up in the project dependencies and the `dev` extra.
2. The third party top level modules imported by `scripts.ci_mutation.plan`,
   `scripts.ci_mutation.__main__` and every `scripts` or `tests` module they
   reach equal the `PLANNER_PACKAGES` import names. Module level imports count,
   including those inside `if`, `try`, `with` and class blocks. Inside a
   function only `scripts` and `tests` imports are followed, because a third
   party import there loads only when that function runs.

When a planner module gains a module level import of another third party
package, change three places in one pull request:

1. Declare the package in the project dependencies or the `dev` extra of
   `pyproject.toml`, if it is not there yet.
2. Add it to `PLANNER_PACKAGES`, distribution name to import name.
3. Add its `pyproject.toml` specifier to the step, in sorted order. For a
   hypothetical `packaging>=24.0` dependency the step becomes:

```yaml
        run: bash "$RUNNER_TEMP/workflow/.github/scripts/pypi-retry.sh" "Install dependencies" -- uv pip install --system "mutmut==3.6.0" "packaging>=24.0" "pytest>=8.0" "pyyaml>=6.0" "tomlkit>=0.12"
```

A changed specifier in `pyproject.toml`, such as a new mutmut pin, also needs
the same change in the step, or the test fails.

### Inspect the plan job

List the plan job's steps with their times:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/RUN_ID/attempts/1/jobs?per_page=100" --paginate \
  --jq '.jobs[] | select(.name == "mutation-plan") | .steps[] | [.name, .conclusion, .started_at, .completed_at] | @tsv'
```

The job log shows the install and the plan it fed:

```bash
JOB_ID=$(gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/RUN_ID/attempts/1/jobs?per_page=100" --paginate \
  --jq '.jobs[] | select(.name == "mutation-plan") | .id')
gh api "repos/the-cloud-clockwork/agentihooks/actions/jobs/$JOB_ID/logs" \
  | grep -E "Resolved|Installed|Mutation shards|Stats parts"
```

On run 39550510897, the first sampled run after the change:

```text
Resolved 20 packages in 273ms
Installed 20 packages in 23ms
Mutation shards: 1
Stats parts: 1
```

### Plan job dependency troubleshooting

| Symptom | Cause and fix |
|---|---|
| **Size mutation shards from stored timings** or **Grade the empty change set** fails with `ModuleNotFoundError` | A planner path imports a package the step does not install. A module level import also fails the contract test; an import inside a function does not. Add the package as described in [Adding a planner dependency](#adding-a-planner-dependency). |
| The contract test fails on the `run` line | The step and `pyproject.toml` disagree on a specifier, the order or the retry wrapper. Copy the specifier from `pyproject.toml` into the step. |
| The contract test fails on the import set | A planner module gained or dropped a third party import. Add or remove the package in `PLANNER_PACKAGES` and in the step. |
| The test raises `KeyError` with a package name | A `PLANNER_PACKAGES` entry is declared in neither the project dependencies nor the `dev` extra. Declare it there. |
| **Install dependencies** is skipped | The job reused the branch preflight report; no plan was computed. |
| The step fails with `Python package index unavailable` | The package index did not answer within the retry window. This is not a code verdict; rerun the job. |

### Plan job dependency delivery evidence

Pull request 5439 merged on 21 October 2026 at 05:02 UTC and closed
[issue 5437](https://github.com/the-cloud-clockwork/agentihooks/issues/5437).
Its [red run 39543828313](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39543828313)
on the tests only head failed only the new contract test, which found the full
`.[dev,all]` install. Its
[green run 39544257187](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39544257187)
installed 20 packages in about one second in a 54 s plan job and passed Gate
Required. The retry wrapper came later, with
[pull request 5665](https://github.com/the-cloud-clockwork/agentihooks/pull/5665);
[pull request 6020](https://github.com/the-cloud-clockwork/agentihooks/pull/6020)
then took the helper from the workflow revision.

Both samples are ten successful first attempts of pull request Tests runs
whose plan job ran **Install dependencies**, with one statistics part and the
`mutation` job skipped. Plan to grade is the time from the plan job's start to
the end of `mutation-stats (0)`.

| Measure | Before, median and mean | After, median and mean |
|---|---|---|
| Plan job wall | 60.5 s, 61.1 s | 49.5 s, 51.6 s |
| **Install dependencies** step | 8.5 s, 8.3 s | 1.0 s, 0.9 s |
| Plan to grade | 82.5 s, 88.2 s | 77.5 s, 93.6 s |

Before runs, recorded by the source task, were created on 21 October from
00:00 to 03:13 UTC: 39518577449, 39521917584, 39521996438, 39524995859,
39529046356, 39529540156, 39529843243, 39530006213, 39536567127 and
39537333298. After runs, sampled for this page, were created on 21 October
from 05:36 to 11:02 UTC: 39550510897, 39551689495, 39554637694, 39554885886,
39555030857, 39571379733, 39574555819, 39578496930, 39580532724 and
39588941249.

The install fell by about 7.5 s per run. The plan job wall fell by 11 s at the
median, more than the install saving, so runner variation also moved it. Most
of the job is not the install: on run 39550510897 the 44 s job spent 9 s in
job setup, 12 s in checkout, 1 s installing and 14 s in **Size mutation shards
from stored timings**. Plan to grade is dominated by the statistics job, which ran 50 to
163 s from plan start in the after sample, and shows no change attributable
to this pull request.

To repeat the measurement on another window, list the runs and compute the
three numbers per run:

```bash
R=repos/the-cloud-clockwork/agentihooks
gh api "$R/actions/workflows/test.yml/runs?event=pull_request&status=success&created=2026-10-21T05:02:17Z..2026-10-21T16:00:00Z&per_page=100" --paginate \
  --jq '.workflow_runs[] | select(.run_attempt == 1) | .id' |
while read -r id; do
  gh api "$R/actions/runs/$id/attempts/1/jobs?per_page=100" --paginate --jq '.jobs[] | select(.name | startswith("mutation"))' |
    jq -rs --arg id "$id" 'def t: fromdateiso8601;
      (map(select(.name == "mutation-plan"))[0]) as $p
      | (map(select(.name == "mutation-stats (0)"))[0]) as $s
      | (map(select(.name == "mutation"))[0]) as $m
      | ([$p.steps[]? | select(.name == "Install dependencies")][0]) as $i
      | select($p and $s and $m and $m.conclusion == "skipped" and $i.conclusion == "success")
      | "\($id) plan=\(($p.completed_at|t) - ($p.started_at|t)) install=\(($i.completed_at|t) - ($i.started_at|t)) grade=\(($s.completed_at|t) - ($p.started_at|t))"'
done
```

The `mutation` job is skipped only when the plan had one shard and one part,
so the filter keeps one shard runs and drops plans with more statistics
parts. The same expressions reproduce the source task's before figures, for
example 69, 9 and 99 s on run 39537333298.

## Suite speed contracts in the branch preflight

A branch push that changes a test module runs the suite speed contracts in the
preflight `plan` job before report reuse, statistics or any mutation shard. A
contract break then fails the preflight in about a minute, instead of passing
mutation and failing later in the pull request unit shards.

### When the contracts run

The `plan` job runs **Find a changed test module** after checkout. It lists
the files changed between `BASE` and the branch head with
`git diff --name-only --no-renames "$BASE...HEAD"`; on a branch push `BASE` is
`origin/dev`. The contracts run when any listed path matches
`^tests/(.+/)?test_[^/]*\.py$`.

| Push | Contracts in the preflight |
|---|---|
| Branch adds or edits `tests/test_quota.py` | Run |
| Branch deletes `tests/swarm_ledger/test_pages.py` | Run; a deleted path is still listed |
| Branch renames a test module | Run; `--no-renames` lists both paths |
| Branch changes only `tests/conftest.py`, `tests/fixtures/...` or code under `hooks` and `scripts` | Skipped |
| Branch already has an open pull request into `dev` | Skipped with the rest of the preflight; the pull request checks own the work |
| Push to `dev`, `main`, `wip/**`, `diffcheck/**`, `promotion/**` or a merge queue branch | The workflow does not run |
| `workflow_call` from another workflow | Skipped; the step runs only for `push` events |

There is no setting, input or environment variable to switch the step. A
failure to compute the diff fails the step rather than skipping the contracts.

### What runs and what a break skips

When a test module changed, the job installs Python 3.12, pinned uv and the
`.[dev,all]` dependencies, then runs:

```bash
python -m pytest tests/test_suite_speed.py --tb=short -p no:cacheprovider
```

The later **Install dependencies** step is then skipped, because the same
environment already holds them. `tests/test_suite_speed.py` holds these
contracts:

| Test | Contract |
|---|---|
| `test_suite_never_runs_the_real_deps_preflight` | Under the suite, the dependency preflight finds no manifest even when the linked bundle holds `deps.json` |
| `test_fakeredis_test_modules_import_without_loading_redis` | Importing every test module that names `fakeredis` loads neither `fakeredis` nor `redis` |
| `test_fakeredis_test_modules_run_on_every_worker` | No test module that names `fakeredis` carries an `xdist_group` mark in its module `pytestmark` |

A failing contract fails **Run the suite speed contracts** and the `plan` job.
The size step never runs, so `stats`, `refine`, `mutation-shard` and
`mutation` are skipped and the workflow concludes `failure`. `touched-tests`
still runs, because it depends only on the open pull request check. The pull
request tier keeps its own copy of these contracts; see
[Delivery tiers](delivery-tiers.md).

A test module that passes both fake Redis contracts imports `fakeredis` inside
a fixture and keeps any worker group mark off the module:

```python
import pytest


@pytest.fixture
def redis_client():
    import fakeredis

    return fakeredis.FakeRedis()


def test_round_trip(redis_client):
    redis_client.set("k", "v")
    assert redis_client.get("k") == b"v"
```

A top level `import fakeredis`, or
`pytestmark = pytest.mark.xdist_group("redis")`, fails the preflight.

### Inspect the contract step

List the preflight runs for a pushed head and read the failing step. Replace
`HEAD_SHA` and `RUN_ID`:

```bash
gh api --method GET \
  'repos/{owner}/{repo}/actions/workflows/mutation-preflight.yml/runs' \
  -f event=push \
  -f head_sha=HEAD_SHA \
  --jq '.workflow_runs[] | {id, head_branch, status, conclusion, html_url}'

gh api 'repos/{owner}/{repo}/actions/runs/RUN_ID/jobs' \
  --jq '.jobs[] | {name, conclusion}'

gh run view RUN_ID --log-failed
```

A contract break shows `plan` as `failure`, `touched-tests` as `success` and
the statistics and shard jobs as `skipped`. The failed log names each failing
contract, for example
`FAILED tests/test_suite_speed.py::test_fakeredis_test_modules_import_without_loading_redis - AssertionError: assert ['fakeredis', 'redis'] == []`.
The assertion message maps each loaded library to the first test module that loaded it.

### Delivery evidence

[Pull request 4388](https://github.com/the-cloud-clockwork/agentihooks/pull/4388)
added the step; it merged into `dev` on 2026-10-18 at 09:52 UTC and applies to
branch pushes from then on. Its proof planted the same test module with a top
level `import fakeredis` on both sides:

| Side | Run | Result |
|---|---|---|
| Plant on `dev` before the change | [39098955827](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39098955827) | Preflight `success`; no contract step, break not caught |
| Plant on the change | [39099046126](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39099046126) | `plan` failed at **Run the suite speed contracts** 32 seconds after the run started; both fake Redis contracts failed; statistics and shards skipped; the whole run ended after 74 seconds |

On a clean head, [run 39098970804](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39098970804)
ran the contracts in about ten seconds and passed; the whole `plan` job took
54 seconds against its ten minute timeout. The contract names in these runs
predate the current worker group rule, which now refuses the mark instead of
requiring one.

The source task counted branches whose push preflight passed and whose pull
request Tests run then failed a suite speed contract. The day before the change,
2026-10-17 05:00 to 2026-10-18 05:00 UTC, read 1 such branch across 117 failed
pull request Tests runs. No count for the day after the merge is recorded.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Preflight fails at **Run the suite speed contracts** | A contract broke. Move the `fakeredis` or `redis` import into a fixture or test, or remove the module's `xdist_group` mark, then push again |
| The step reads `skipped` although a test changed | The changed file does not match `tests/.../test_*.py`, such as `conftest.py` or a helper module, or the branch has an open pull request into `dev`. The pull request tier still runs the contracts |
| **Import probe exited** with a traceback | A test module that names `fakeredis` fails to import at all. Fix the import error the stderr shows |
| Preflight passed, then the pull request shards failed a contract | The preflight ran before the break was pushed, or skipped the step for one of the reasons above. Read the pull request run's failing test name; the fix is the same |

## Measured shards and split mutants

The preflight collects shared function-to-test coverage and test durations, then its `refine` job builds the measured plan. Expensive mutants are placed first on the least loaded shard. A survivor must run all its covering tests, which can leave one shard running long after shards whose mutants were killed early.

The planner splits a heavy function's covering tests into a shared fast head and balanced tail slices. Every mutant of that function runs as several units: each unit runs the head plus one slice on a distinct shard. The head repeats so tests that kill mutants quickly remain available on every unit; the slices together retain every remaining covering test. Other mutants keep one shard assignment.

These are implementation defaults, with no environment variable or workflow dispatch input to tune them:

| Setting | Default | Meaning |
|---|---|---|
| `HEAD_SECONDS` | 20 traced seconds | Select the fastest covering tests up to this budget, always including at least one test when available |
| `SLICE_SECONDS` | 120 traced seconds | Divide the remaining duration by this value, rounding up to choose the slice count |
| Split limit | 10 slices | Cap a mutant's units; fewer than two slices leaves it unsplit |
| Shard target and limit | 240 seconds, 10 shards | Size the plan using estimated work and the [batch runner's planned workers](#shard-planning-from-the-batch-runner-cpu-quota), ensuring enough shards to place a mutant's units separately |

For example, ten covering tests of one second each form a ten-second head. Thirty more tests of twenty seconds each make a 600-second tail, split into five 120-second slices. Each unit runs the ten head tests and six tail tests on its own shard. All forty distinct tests remain covered, although the head runs five times. Unit cost is estimated as `5.6 + 1.25 * (head_seconds + slice_seconds)`, or 168.1 worker seconds here. A split mutant's total estimated cost includes every repeated head.

Repeated measurements use the maximum observed duration; a missing duration uses at least 5.6 seconds or the largest observed test duration, whichever is greater. A test stays whole, so a single slow test or the ten-slice cap can exceed the slice target. Traced duration and estimated worker seconds are planning inputs, not a wall-time deadline or a guarantee that jobs finish together.

### Pricing from recorded killing tests

mutmut stops a mutant at its first failing test, and a shard runs the test that last killed a mutant of the same function first. The planner prices an unsplit mutant the same way: from that one test, not from every test traced to its function.

| What the plan knows about the mutant's function | Estimated cost |
|---|---|
| A recorded killing test that is among the function's traced tests | `5.6 + 1.25 * killer_seconds` |
| No recorded killing test, or one the function no longer traces | `5.6 + 1.25 * traced_seconds`, the sum of every traced test |
| No traced tests | `5.6 + 1.25 * max(60, largest function sum)` |
| The function is split into units | The unit costs above, unchanged |

For example, a function traced by tests of 2, 8 and 30 seconds costs 55.6 worker seconds per mutant without a killer. Once the 2 second test has killed one of its mutants, each mutant costs 8.1. Durations follow the same rules as split planning: the maximum observed duration, at least 5.6 seconds or the largest observed duration when a test has none. Shard sizing, the shard target and limit, and the 1080 second shard budget are unchanged; a lower estimate places more mutants on fewer shards.

The preflight `stats` parts also keep each test file's statistics in one merged `mutation-test-stats-v2-<os>-` cache per run, which every part restores, so an unchanged test file is not traced again even when it moves to another part. A single part saves the cache itself; with more parts each uploads its cache folder and `refine` merges them and saves the cache once after all parts finish. An entry is reused when the gate code, configuration, runner options, installed packages (the project itself by its release, since its build number changes with every commit), the test file and everything it imports or names as a file are unchanged, together with the changed lines of each source among them. A file that reaches a dynamic import whose target is not a literal is also checked against every repository module its statistics bucket loaded and what those import or name; a collection that recorded no modules falls back to every Python module in the repository. Each part prints `Mutation stats cache: reused N test files; collect M` in its **Collect one stats part for every mutation shard** step.

Only the branch preflight (`mutation-preflight.yml`) carries killing tests between runs. A kill is recorded per function as the failing test's node id, in a file named by a digest of the function name. The preflight keeps them per branch in an actions cache:

| Job | Killing test step |
|---|---|
| `stats`, when it plans as a single job, and `refine` | Restore the newest `mutation-killers-v1-<os>-<branch>-` cache before building the plan |
| `mutation-shard` | Restore the same cache; `seed_measured_shard` copies it into the shard work folder, so the shard runs the priced test first. After mutating, upload this shard's kills as `mutation-killers-<shard>` |
| `mutation` | Restore the cache, add every shard's kills on top, and save the union under a key naming this run and attempt |

A branch's first preflight has no cache and prices every mutant from its traced tests; its second run is the first to use kills. The kill artifacts are kept for one day. The pull request Tests run plans without a kill cache, though each of its shards still orders tests by the kills it records during that run.

Inspect a preflight's kill handling in its job logs:

| Log line | Where | Meaning |
|---|---|---|
| `Cache restored from key: mutation-killers-v1-...` | Restore steps | Earlier kills reached this job |
| `Cache not found for input keys: ...` | Restore steps | First run on the branch, or the cache expired |
| `Measured mutation worker seconds: N` | `refine` | The plan's total estimate, with kill pricing applied |
| `Functions with a recorded killing test: N` | `mutation-shard`, **Collect the tests that killed mutants** | Kills this shard uploads |

```bash
gh run view RUN_ID --log | grep -E 'mutation-killers-v1|Measured mutation worker seconds|Functions with a recorded killing test'
```

When the estimate still runs far over the **Mutate changed Python files** step time, check whether the restore found a cache, then whether the heavy mutants belong to split functions: split units keep their traced cost even when a killing test is known. A cached killer that no longer kills falls back to normal ordering in the shard, and is replaced by the next test that kills.

[Pull request 4753](https://github.com/the-cloud-clockwork/agentihooks/pull/4753) delivered kill pricing and the cache. In [proof run 39237282629](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39237282629) the first attempt found no cache and planned 418.3 worker seconds for 40 mutants against 52 worker seconds of mutation; the rerun restored three killing tests and planned 226.1 against 56. [Seeding proof run 39239742322](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39239742322) showed `refine`, all eight shards and the merge job restoring the cache and the merge job saving the union. Before the merge, 625 preflight shards between 18 October 09:50 UTC and 19 October 09:50 UTC had a median planned over actual ratio of 9.77, and 6.72 for shards without split units. The remaining floor is the fixed 5.6 seconds per mutant against about 1.4 seconds observed in the proof run.

### Test durations adjusted for statistics contention

When a statistics job runs more buckets than its runner's CPU quota, as it did before [statistics and shard worker sizing](#mutation-statistics-and-shard-worker-sizing) or on a runner with no finite quota, buckets wait on each other and every recorded test duration carries that wait. Before pricing, the planner divides each recorded duration by the contention ratio of the statistics it read:

| Input | How the planner reads it |
|---|---|
| Recorded wall per test | The largest duration recorded for that test across the statistics results, so a test repeated by overlapping cached entries counts once |
| CPU per result | `cpu` in each statistics result: the bucket's process CPU seconds, or for a cache entry an equal share of the fresh collection that wrote it |
| Results used for the ratio | Only results with a nonzero `cpu` and at least one recorded duration; empty buckets and results without `cpu` are left out |
| Contention ratio | `max(1, sum of recorded walls / sum of CPU)`, computed separately for each statistics group the plan loads; `1` when no result qualifies |

The adjusted duration, `recorded / ratio`, replaces the recorded one everywhere the measured plan uses durations: unsplit mutant cost, killing test cost, the fallback for a test with no duration (at least 5.6 seconds or the largest adjusted duration), the no traced tests fallback, and split head and slice sizing. No test leaves a mutant's covering set, and shard execution, grading and the 1080 second shard budget are unchanged; only the estimates fall, so the plan can place mutants on fewer shards and split fewer functions.

For example, one bucket records tests of 6 and 12 seconds with 6 CPU seconds, and a second bucket records an 18 second test with 6 CPU seconds. The ratio is 36 / 12 = 3. A function traced by the 6 and 18 second tests was priced at `5.6 + 1.25 * 24 = 35.6` worker seconds per mutant; with adjusted durations of 2 and 6 seconds it costs `5.6 + 1.25 * 8 = 15.6`. A ratio below one, where CPU exceeded wall, is floored at one, so durations are never inflated.

There is no setting for the ratio, and no log line prints it. Compare `Measured mutation worker seconds` and `Measured mutation shards` in the `refine` log against an earlier run on the same head, or recompute the ratio from the downloaded statistics results: sum each test's largest duration across the results that carry `cpu`, then divide by the summed `cpu`.

| Symptom | Check |
|---|---|
| The estimate did not fall after this change | Statistics results without a nonzero `cpu` or without durations are left out of the ratio; with no qualifying result the ratio is one |
| The estimate is below the observed **Mutate changed Python files** step time | The ratio removes waiting recorded during statistics, while mutation shards add their own instrumentation and setup; compare planned over actual on the same run before treating it as a regression |
| One group's mutants are priced differently from another's | Each statistics group the plan loads has its own ratio |

[Pull request 4786](https://github.com/the-cloud-clockwork/agentihooks/pull/4786) delivered the adjustment. Replaying the planner on the same statistics artifacts, the account scan head of [run 39207424222](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39207424222) fell from 48.2 to 16.2 estimated minutes per mutant and from 100 to 60 units on ten shards; five other recent pull request runs fell between two and four times, and two of them needed fewer shards (10 to 6, and 5 to 3). Predicted over actual shard wall stayed between 1.6 and 4.9 across all six runs, so the estimate still exceeds observed work. On the account scan head every one of the 1881 traced tests executes the changed one statement function, so line level narrowing could not have cut its test set.

### Combined grading

A shard grades its unsplit mutants normally and records split results under `units` for the final `mutation` merge job. A successful unit or shard alone does not establish a successful gate.

| Unit outcomes for one mutant | Combined outcome |
|---|---|
| Any unit killed it | Killed, counted once |
| Every unit survived | Survived, evaluated against changed lines and the checked out clearances |
| No kill and an abnormal outcome, such as a timeout | Preserve an abnormal outcome for normal failure evaluation |
| A shard report is missing or a shard did not finish successfully | Fail the workflow |

Splitting changes execution units, not the selected mutant set or the clearance fingerprint. An uncleared combined survivor on graded lines still fails. Per-shard `expected` and `graded` count assigned units, so their sum can exceed the number of distinct mutants; use the merged file counts when comparing distinct mutant totals. Retained mutation replay uses the same split selection and combined grading, and checks completeness against the planned unit count.

### Inspect a preflight

Download evidence from the preflight run being investigated into a task work folder. Replace `RUN_ID` and `EVIDENCE_DIR` with that run and folder:

```bash
gh run download RUN_ID \
  --name mutation-plan \
  --dir EVIDENCE_DIR/plan

gh run download RUN_ID \
  --name mutation-preflight-report \
  --dir EVIDENCE_DIR/merged

gh run download RUN_ID \
  --pattern 'mutation-preflight-shard-*' \
  --dir EVIDENCE_DIR/shards

jq '{shards, shard_worker_seconds, assignments, splits}' \
  EVIDENCE_DIR/plan/plan.json

jq '{failed, shards, files}' \
  EVIDENCE_DIR/merged/report.json
```

In `assignments`, an integer is an unsplit mutant's shard; an array maps each slice position to its shard. `splits` records the function's head, slices and unit costs. Shard reports record `wall_seconds`, `expected`, `graded`, `complete`, `budget_exhausted` and estimated worker seconds. Plan and report artifacts are retained for seven days. A reused report or a run with no changed Python may have no measured plan or shard artifacts; inspect the merged evidence and run steps first.

When investigating uneven work, compare the **Mutate changed Python files** step durations within one run, excluding setup and runner waits. Inspect the heavy function's slices and shard completeness before comparing distinct mutant counts. Missing reports, incomplete grading and budget exhaustion are failures to investigate; shard balancing retains every selected mutant and covering test.

For a retained replay, the Proofs workflow takes the retained case through its `base` input:

```bash
gh workflow run proofs.yml \
  --ref YOUR_BRANCH \
  -f proof=mutation-replay \
  -f base=third
```

The replay uploads `mutation-replay-proof` with the plan and completeness evidence. It checks a retained case rather than measuring current branch preflight timing.

### Delivery evidence and timing limits

[Pull request 4025](https://github.com/the-cloud-clockwork/agentihooks/pull/4025) delivered splitting and combined grading, with [successful branch preflight 39028535693](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39028535693). Its planning replay of run 38999090987 divided the slow survivor's function into nine units of about 173 estimated worker seconds and estimated shard loads between 3000 and 3108 worker seconds. Those numbers describe the plan, not observed shard completion times.

The source acceptance target is a slowest mutation shard within sixty seconds of the next slowest, preserving the graded mutant set. The recorded comparison covered twenty preflight runs before and twenty-one after: the maximum gap between the slowest and next slowest shard fell from 369 to 18 seconds. Three earlier runs exceeded sixty seconds; none of the later runs did. All later runs reported complete grading with expected and graded unit counts equal; two earlier runs were incomplete. These are observations across different branch workloads, rather than a replay of identical mutant sets.

For example, [earlier run 38999090987](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38999090987) recorded shard walls of 399 and 30 seconds for its slowest two shards. [Later run 39052293854](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39052293854) recorded 212 and 194 seconds, an eighteen-second gap, with all 1656 planned units graded. The target compares the slowest two shards, not the spread across all shards or their absolute completion time. See [Tests workflow timing](../ci-timing.md) for the surrounding CI timing context.

## Graded lines

A branch is graded only on its own lines, so a dev refresh or a stacked branch does not pay for work that was already graded:

- **Real bases.** The plan job resolves them once:
  - the merge base with the given base;
  - the merge base with `origin/dev`;
  - every pushed commit inside the head whose own push Mutation preflight had a successful `mutation` job, or, for an open pull request, whose Tests run graded every shard, inline or in a shard job (see [Gate Required and graded bases](#gate-required-and-graded-bases)).
- **Which lines count.** A line is graded only when it differs from every real base. Because the given base always counts, the scope never grows past `base...head`.
- **Proof of grading.** The plan reads the preflight proof through the Actions API on the App token. Dispatched runs never count, since they choose their own base.
- **Weakened tests.** The narrowing never hides weakened tests. If the head changed `tests/` against a newer base beyond adding tests, every line that base excluded is graded again. Adding tests means new `test_` functions, or new `Test` classes holding only test methods, with only `pytest.mark` decorators, or imports in a new test file, and no reused or repeated name. An import added to an existing test file counts as a change, since it can shadow a fixture or builtin those tests use. Anything else counts as a change: an edit, a deletion, a rename, a skip marker, a fixture, a helper, a conftest or a data file.
- **One list for every job.** The plan writes the resolved list as `bases`. The stats parts, shards, push preflight and the proofs mutation take it with `--bases`, so every job grades the same mutants.

- **Reuse on the push preflight.** Before the stats and shard legs take a batch runner, each under a concurrency group of its own run that the proofs workflow and other dispatches never wait on (see [Mutation preflight legs on the batch pool](../ci-timing.md#mutation-preflight-legs-on-the-batch-pool)), the cheap decision job reads the newest graded push preflight for the same head on any branch, then the branch's newest. When that run concluded success with a complete green report and this head equals it or only adds tests since, as defined above, its report is uploaded as this run's `mutation-preflight-report` with a `reuse.json` naming the source run, and the mutation job is skipped. A newer red or failed run, a missing report, any change outside `tests/` or any lookup error runs the mutation job as before, so no head goes green without a complete green report. A reused head never counts as a graded base. Measured on PR 2624: the full preflight 38151448161 took 116 s; the tests only head after it reused that report in run 38151636224, whose decision job took 18 s.
- **Forwarding in the pull request plan.** When the pull request plan reuses the branch preflight report of its exact head, or its plan finds no changed Python file and grades that empty set itself, the plan job uploads the report as `mutation-report-0`. The `mutation-stats` and `mutation` jobs then run none of their own steps and still succeed, so Gate Required is unchanged. Measured on PR 3965: an empty change set spent 8 s in stats and 9 s in mutation in run 39019862061, against 25 s and 28 s in run 39018870323, with the same report. [Reused and empty pull request runs](#reused-and-empty-pull-request-runs) covers the conditions, what a forwarded run looks like and how to inspect one.

Measured on dispatched proofs:

| Case | Old scope | New scope | Shard time |
|---|---|---|---|
| Branch refreshed with dev, base pinned to an older dev commit | 17 files | 203 own mutants | 39 s |
| Branch stacked on a graded branch | earlier branch's 303 mutants plus own | 13 own mutants | 23 s |

On the pull request that delivered this, the whole mutation chain finished 184 s into the Tests run: the plan, including the App token mint, took 21 s, and the stats and two shards finished by 184 s. Gate Required finished at 431 s, well inside the fifteen minute budget. Sonar and the coverage ratchet set that time, not mutation.

## Reused and empty pull request runs

On a pull request into dev and on a dispatched Tests run, mutation runs as three jobs of the Tests workflow: `mutation-plan`, then the `mutation-stats` matrix, then the `mutation` shard matrix. All three are needs of Gate Required. Two cases need no mutant work, so the plan job forwards the final report, the preflight's for an exact head reuse and its own grading of an empty set otherwise, `mutation-stats` succeeds after its runner setup steps, and `mutation` is skipped as for a [single shard plan](#single-shard-plans-in-the-statistics-job). Exact head reuse applies only to pull requests; the empty change set path also applies to dispatched runs.

### When the plan forwards

The plan job sets its `forwarded` output to `true` in either case:

| Case | Condition | Report in `mutation-report-0` |
|---|---|---|
| Exact head reuse | The reuse step finds a green branch preflight report for this exact head and base. | The preflight's `report.json` and evidence without its `scope.json`, plus `reuse.json` naming the source run and head |
| Empty change set | Reuse found nothing, and `python -m scripts.ci_mutation.plan` reports `changed=0`: no changed Python file in `hooks` or `scripts` that still exists at the head, against the graded bases. A pull request that only deletes Python files takes this path. | The plan job's own grading of the empty set, run as `python -m scripts.ci_mutation --bases "$BASES" --budget 1080` |

Exact head reuse runs `scripts.ci_mutation.reuse` from a detached checkout of the pull request base commit, so a branch cannot change the code that decides reuse. It reuses only when all of these hold:

- The files that produce a preflight report are identical between that base commit and the head: `.github/workflows/mutation-preflight.yml`, `.github/workflows/test.yml`, `.github/actions/`, `.github/test-excludes.txt`, `pyproject.toml` and `scripts/ci_mutation/`.
- The lookup walks completed push preflights on this branch for this head, newest first, and ignores cancelled and skipped runs. It takes the first run that concluded success with an unexpired `mutation-preflight-report` artifact; a success whose artifact is missing or expired is passed over for an older one. A newer run with a red report, or one that concluded failure, stops the walk and blocks reuse.
- That report's `report.json` has no failed file, no file left unmutated and no file with failures.
- Its `scope.json` names this head and the merge base of the head with `origin/dev`.

A pull request whose head is a tests-only addition on top of a graded head does not qualify: unlike the push preflight's own reuse, the pull request plan requires the exact head. The pull request lookup does not wait for a preflight still running on the head; it reads only completed runs. A lookup error prints `Reuse lookup failed, mutating instead` and the run takes the normal path.

Everything else runs unchanged. A change with one or more changed Python files still collects stats in every planned part and mutates in every planned shard, so the selected tests and the statistics match a run before forwarding existed.

### Planned test selection and older heads

The Tests workflow can come from the pull request merge ref while its mutation
planner runs from the head being tested. An older head can therefore run under
a newer workflow without producing the new planned test selection. The
`mutation-plan` job checks this contract before uploading anything.

The planner's file is `.mutation-selection.json`; its Actions artifact is
`mutation-selection`. These are separate from the mutation report artifacts.
The compatibility check applies whenever the plan is not reusing an exact head
report.

| Planner output | Contract result | Following jobs |
|---|---|---|
| File absent | Succeeds with `selection=false` and explains the fallback in the job summary | Skips selection upload and downloads; mutation selects matching and importing test modules as before |
| Valid JSON object whose values are lists of strings | Succeeds with `selection=true` | Uploads `mutation-selection`; stats and mutation download it when `forwarded` is not `true` |
| File present but invalid JSON, a non-object, or an object containing a value that is not a list of strings | Fails with `.mutation-selection.json is malformed.` | No successful plan proceeds to stats or mutation |

The missing file path preserves test selection and mutation grading for older
producers. It does not waive the mutation gate. A malformed file must be fixed
in the producer; removing it to hide the failure discards the planned selection.
For reused or empty reports, the [forwarding behavior](#when-the-plan-forwards)
still applies, and forwarded jobs do not download a selection.

#### Format and configuration

This is an automatic workflow contract; it has no operator environment variable
or dispatch switch. The planner produces the file in the job checkout, and the
workflow carries it between jobs. Contributors changing the planner should emit
a JSON object mapping source modules to test module lists, for example:

```json
{
  "scripts/example.py": ["tests/test_example.py"],
  "scripts/another.py": []
}
```

An empty object and empty test lists pass this shape check. The check validates
types; it does not prove that paths exist or that the selected tests cover the
changed behavior. A scalar value such as
`{"scripts/example.py": "tests/test_example.py"}` or a list containing a number
fails. Keep the upload and both downloads gated by the plan's `selection`
output when changing the workflow. Upload remains strict when a selection was
promised: `if-no-files-found: error`.

#### Inspecting compatibility and failures

Open the Tests run's `mutation-plan` job and read **Check the planned selection
contract**. For an absent file, its summary says:

```text
This head's planner predates the planned test selection, so mutation selects matching and importing test modules as before.
```

Inspect a run without starting another one:

```bash
gh run view RUN_ID --log --repo the-cloud-clockwork/agentihooks
gh api 'repos/the-cloud-clockwork/agentihooks/actions/runs/RUN_ID/artifacts' \
  --jq '.artifacts[] | {name, expired}'
```

Replace `RUN_ID` with the Tests run being investigated. When the contract
publishes a selection, download it into your task evidence folder:

```bash
gh run download RUN_ID --repo the-cloud-clockwork/agentihooks \
  --name mutation-selection --dir EVIDENCE_DIR
cat EVIDENCE_DIR/.mutation-selection.json
```

| Symptom | Interpretation and action |
|---|---|
| No selection artifact and the older planner summary is present | Expected compatibility fallback; inspect stats and mutation results for the actual grade |
| No selection artifact on a reused report | Expected; inspect the forwarded report and reuse evidence instead |
| Contract step reports malformed selection | Read the producer output and correct its JSON or value types; this case never falls back |
| Selection was promised but upload or download fails | Inspect the transfer step and artifact availability; the fallback covers an absent planner file, not a failed artifact transfer |

The delivered change is [pull request 4560](https://github.com/the-cloud-clockwork/agentihooks/pull/4560).
Its incident was [Tests run 39118631511](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39118631511),
which failed at **Upload planned mutation test selection** because the older
head did not write the file. Regression coverage exercises absence, valid and
malformed selections, transfer conditions and the runner's previous selection
behavior.

### What a forwarded run shows

| Job | Forwarded run | Normal run |
|---|---|---|
| `mutation-plan` | Ends with `Forward the reused or empty mutation report`; a reused run skips installing dependencies and planning | Reuse lookup and planning; the forward step is skipped |
| `mutation-stats (0)` | Succeeds after its runner setup and post steps; no `mutation-stats-*` artifact | Collects one stats part per matrix entry and uploads `mutation-stats-<part>` |
| `mutation` | Skipped, since both matrices are `[0]`; uploads nothing | Mutates its shard and uploads `mutation-report-<shard>`, or is skipped when a [single shard plan](#single-shard-plans-in-the-statistics-job) mutates inside `mutation-stats (0)` |
| Gate Required | Accepts the skipped `mutation` job because both outputs are `[0]` | Reads the same three job results, or the inline form for a single shard plan |

A reused run sets both matrices to the single entry `[0]`. An empty change set gets `[0]` from the plan, since it has no mutants to split.

The plan log names the path taken:

```text
Reused the green mutation report of run <preflight run id> for <head sha>
```

or, for an empty change set, with the last line printed by the `Grade the empty change set` step:

```text
No green mutation report covers this head; mutating
Mutation bases: <base sha>
Mutation shards: 1
Stats parts: 1
Mutation shard: 1 of 1
```

The empty set's `report.json` is:

```json
{"files": [], "not_mutated": [], "failed": false}
```

Measured on the docs only pull request #4121 after this shipped, run 39047441340: `mutation-plan` 35 s, `mutation-stats (0)` 7 s with 2 of 12 steps run, `mutation (0)` 8 s with 2 of 13 steps run, and a 178 byte `mutation-report-0` holding the report above. Before forwarding, the same kind of run, 39018870323, spent 25 s in stats and 28 s in mutation.

### Inspecting a run

List the mutation jobs and how many steps each ran:

```bash
gh api "repos/{owner}/{repo}/actions/runs/<run id>/jobs?per_page=100" \
  --jq '.jobs[] | select(.name | test("^mutation")) | "\(.name) \(.conclusion) \([.steps[] | select(.conclusion == "success")] | length)/\(.steps | length) steps"'
```

Download the forwarded report:

```bash
gh run download <run id> -n mutation-report-0 -D mutation-report
cat mutation-report/report.json
cat mutation-report/reuse.json
```

`reuse.json` is present only for exact head reuse.

### Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `mutation-plan` fails at `Forward the reused or empty mutation report` with no files found | The forward step requires `.mutation-gate/`, and the reuse unpack or the empty grading wrote nothing | Read the reuse and `Grade the empty change set` step logs; Gate Required stays red because `mutation-stats` and `mutation` do not start after a failed plan |
| A branch whose push preflight passed still runs the full stats and mutation jobs | The head changed one of the producer files listed above, the preflight graded another head or base, its report artifact expired, a newer preflight on the head failed, or the preflight had not completed when the plan looked | Expected; the run grades the change itself |
| `mutation-plan` fails at `Grade the empty change set` | The empty grading exited nonzero | Fix the error named in the step log; the forward step still runs, and when it finds no files the grade step log is the evidence |
| `mutation-stats` or `mutation` skipped rather than succeeded | `mutation-plan` did not succeed | Fix the plan failure; on a pull request into dev Gate Required needs all three mutation jobs to succeed, so a skipped one fails it |

`--head`, `--output` and `--budget` override the comparison head, evidence directory and total seconds. The default budget is eighteen minutes, leaving eight minutes for setup, both browser install attempts and evidence upload in the twenty six minute job. Over budget files and failed runs are named and fail the gate. All outcomes, including survivors on untouched lines, are recorded in `report.json`; CI uploads the complete evidence directory on success or failure.

Mutmut 3 mutates functions and methods. Files without mutable functions are reported explicitly with a zero count. A surviving or uncovered mutant fails when its original source lines intersect added or changed hunk lines. Incomplete, timed out and unexpected mutant outcomes fail regardless of their line. Generated trampolines and function relative diffs are mapped back to the original source, including decorators and methods.

A Standards reader clears an exact survivor with one JSON file under `mutation-clearances/`. Each file holds one key combining the source file, mutant name and fingerprint from the report. The filename is the SHA256 of that complete key followed by `.json`, so different rulings can be added by concurrent pull requests without editing a shared object. Every entry requires the reader and its reason. The runner also reads the legacy root `mutation-cleared.txt`; identical duplicates are accepted and conflicting rulings fail. The fingerprint binds the original function and mutant, so a changed function invalidates an old clearance.

```json
{
  "hooks/example.py:hooks.example.x_example__mutmut_1:FINGERPRINT": {
    "reader": "Standards",
    "reason": "The changed argument does not change the observed output."
  }
}
```

Write an independently reviewed ruling with `scripts.ci_mutation.clearances.write_clearance(root, key, ruling)`; `clearance_path(root, key)` returns its deterministic filename. Run `python -m scripts.ci_mutation.clearances` from the repository root to migrate legacy rulings without changing their reader or reason. The migration leaves an empty legacy object for compatible older readers. Trace plans and the build scope gate accept both clearance locations as supporting proof.

## Clean baseline failures

Before it mutates anything, mutmut runs the selected tests against the unmutated copy. When that clean baseline fails, no mutant is graded: every file in the group is reported as not mutated and the gate fails. The run names each failing test with its first error line in three places, so the cause is readable without downloading the run log:

| Where | What it shows |
|---|---|
| Step log, the gate's red message | One `not mutated` line per file in the group, its reason listing every failing test |
| `report.json` | `failing_tests`, a list of failing test ids with their first error line, in the order they first appear |
| Job summary | A `Mutation clean baseline failed` heading followed by one bullet per failing test |

This applies to every job that runs `python -m scripts.ci_mutation`: in the pull request Tests run, each `mutation-stats` part, the inline mutation step of a single shard `mutation-stats` job, and each `mutation` shard. The push preflight follows the same split, a statistics job and then a `mutation-shard` job, with no inline step. Which tests run and how mutants are graded are unchanged.

### How failing tests are read

The runner reads the group's `run.log`, the output of the mutmut selection step, after that step exits nonzero. It keeps every line that starts with `FAILED` or `ERROR` and names a `.py` test id, which is pytest's short test summary format:

```text
FAILED tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'
ERROR tests/test_sample.py::test_setup - RuntimeError: no server
FAILED tests/test_sample.py::test_bare
```

- The entry is the test id and, when pytest printed one, the text after ` - `: the first error line.
- A test named more than once keeps its first line; a later teardown `ERROR` for the same test is dropped.
- `RERUN` lines and other output are ignored, and undecodable bytes in the log are replaced rather than failing the read.
- With two test groups in one job, `failing_tests` joins both groups' entries and drops exact repeats; each file's reason lists only its own group's tests.

When the log holds no such line, for example an import error before collection, the reason keeps its plain form and the report carries no `failing_tests` key. A selection step that runs out of budget is reported as `over budget`, and a failed report step as `report failed`; neither reads failing tests.

### What a failed baseline shows

The step log of the failing job prints the reason for each file in the group:

```text
hooks/sample.py: not mutated, mutmut failed with exit 1; failing tests: tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'; tests/test_sample.py::test_setup - RuntimeError: no server; tests/test_sample.py::test_bare; see WORKSPACE/.mutation-gate/0-abc123/run.log
```

The same reason is the file's `reason` under `not_mutated` in `report.json`, beside the `failing_tests` list:

```json
{
  "files": [],
  "not_mutated": [
    {
      "path": "hooks/sample.py",
      "reason": "mutmut failed with exit 1; failing tests: tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'; tests/test_sample.py::test_setup - RuntimeError: no server; tests/test_sample.py::test_bare; see WORKSPACE/.mutation-gate/0-abc123/run.log"
    }
  ],
  "failed": true,
  "failing_tests": [
    "tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'",
    "tests/test_sample.py::test_setup - RuntimeError: no server",
    "tests/test_sample.py::test_bare"
  ]
}
```

When `GITHUB_STEP_SUMMARY` is set, as on every GitHub Actions step, the gate appends this to the job summary on the run page:

```markdown
### Mutation clean baseline failed

- `tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'`
- `tests/test_sample.py::test_setup - RuntimeError: no server`
- `tests/test_sample.py::test_bare`
```

Nothing is appended when `failing_tests` is absent or empty, so a run with no clean baseline failure leaves the summary unchanged. Outside Actions, with the variable unset, only the step log and `report.json` carry the names.

### Reading the names from a run

Open the run page to read the job summary, or download the evidence. Every artifact below holds the complete `.mutation-gate/` folder on failure, including each group's `run.log`:

| Workflow | Failed job | Artifact |
|---|---|---|
| Tests, on a pull request | `mutation-stats` part | `mutation-evidence-stats-<part>` |
| Tests, on a pull request | `mutation` shard, or the inline step of a single shard `mutation-stats` job | `mutation-report-<shard>` |
| Mutation preflight, on a branch push | statistics part | `mutation-evidence-stats-<part>` |
| Mutation preflight, on a branch push | `mutation-shard` | `mutation-preflight-shard-<shard>` |

For example, for a failed statistics part:

```bash
gh run download <run id> -n mutation-evidence-stats-0 -D mutation-evidence
jq -r '.failing_tests[]?' mutation-evidence/report.json
jq -r '.not_mutated[] | "\(.path): \(.reason)"' mutation-evidence/report.json
```

The `see` path at the end of the reason is the absolute path on the runner, shown above with `WORKSPACE` for the job checkout; its last two parts, the group folder and `run.log`, locate the log inside the artifact. Read it for the full traceback.

### Clean baseline troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| A file is not mutated with `mutmut failed with exit N; failing tests: ...` | A selected test fails on the unmutated code inside the mutation workspace | Fix that test or the code it exercises; when the test passes in the unit shards, compare the isolated copy with the checkout, such as a fixture file the workspace does not copy, and remember the baseline runs serially |
| The reason ends `mutmut failed with exit N; see .../run.log` with no test named | The log has no `FAILED` or `ERROR` summary line, for example a collection or import error, or a crash before pytest's summary | Read `run.log` from the evidence artifact |
| The job summary has no `Mutation clean baseline failed` heading on a red mutation job | The job failed for another reason: survivors, `over budget`, `report failed`, or a missing statistics part | Read the step log and `report.json` |
| The same failing test is listed for several files | All files in one mutation group share one clean baseline run | Expected; one fix clears every file in the group |

The behavior is pinned by `tests/test_ci_mutation_runner.py`, which feeds a planted pytest summary through `run_gate` and checks the reason, the printed red message and `report.json`, and by `tests/test_ci_mutation_main.py`, which checks the job summary block and that a run without failing tests leaves the summary untouched.
