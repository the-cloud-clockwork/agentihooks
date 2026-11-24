---
title: Test count floor
parent: Reference
nav_order: 10
---

# Test count floor

The `test-count` job in the Tests workflow fails when the head collects fewer tests than its base. It counts collected tests, so it stops a pull request from passing by deleting the tests that would fail it or keeping them from being collected. A skip marker leaves a test collected and is not counted as a loss. One exception lets a change delete dead code together with its tests: a lost test is admitted when the head deletes a top level function, class or constant that the test referenced, or deletes a keyword parameter that selected a branch in a function the head keeps and the test passed that keyword.

## Where it runs

`test-count` runs on Python 3.12 for pull requests, merge groups and dispatched runs that pass admission. Pushes do not run it. It checks out the base revision into a second worktree and runs the base revision's own `tests/count_floor.py`:

```bash
cd "$RUNNER_TEMP/base"
python -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
```

The base revision's copy grades, so a pull request cannot loosen its own floor. A base without `tests/count_floor.py` fails the job. A change to the gate therefore takes two pull requests: the first changes the tests to accept both the old and the new behaviour, the second changes the gate, which grades from the next pull request on.

The base is the merge base of the head with the event's base: `origin/dev` for pull requests into dev, the pull request base, the merge group base, or the `base` input of a dispatched run. The job prints it as `BASE: <sha>`.

## What it compares

The head collects its own `tests/` with `pytest --collect-only`. The base uses a matching [dev suite record](#dev-suite-records) when available; otherwise it collects in its own directory so it imports its own sources. Each side's collection is [split across processes](#collection-across-processes), and when both sides need collection, they run at the same time and share the processes.

- A collection error, or no tests collected, on either side fails the job: `Collecting the base tests failed or found none, so the floor cannot be graded.`
- The floor is the base count minus the admitted lost tests.
- The job passes when the head count is at least the floor. Otherwise it lists every lost test that was not admitted and fails: `The head collects N fewer tests than the base floor of F.`

The floor compares counts. A renamed test passes, since the new id offsets the lost one, and so does any lost test offset by an equal number of added tests.

## Collection across processes

Every collection the gate runs, for the head, a base without a usable record and the dev record producer, is split across several `pytest --collect-only` processes and the results are joined in order. The collected test ids, their order, the count and the floor comparison are the same as a single process collection of the same tree; only the time changes. Before pull request [4864](https://github.com/the-cloud-clockwork/agentihooks/pull/4864) the head collection ran in one process and took 20 to 25 seconds of a 55 to 63 second job.

### Process count

`tests.count_floor.collection_processes` returns the runner's usable CPUs: the CPUs the job may run on, capped by the cgroup CPU quota when one is set (`scripts.ci_cpu_quota.worker_count`, the same rule the unit shards use), and never below one. When the base and the head both need collection, each side gets half of that count, rounded down; a side left with zero or one process collects in a single process. A side read from a dev record or the collection cache starts no process.

| Usable CPUs | CPU quota | Sides collected | Processes per side |
|---|---|---|---|
| 6 | none | head only | 6 |
| 6 | 2.5 | head only | 2 |
| 4 | none | base and head | 2 |
| 2 | none | base and head | 1 |

There is no setting for the process count; it follows the runner.

### How the suite is split

`tests.count_floor.collection_groups` lists the top level entries of `tests/` in name order and gives each a weight: the number of `test_*.py` files beneath a directory, 1 for a top level `test_*.py` file and 0 for anything else. It walks the entries in order and starts a new group when the files placed so far reach the next equal share of the total, so each group is a contiguous run of names with about the same number of test files. An entry with weight 0 never starts a group; it joins the current one. `__init__.py` and `conftest.py` at the top of `tests/` are added to every group, so each process loads the same root fixtures and configuration hooks.

For a `tests/` holding `alpha/` (two test files), `beta/` (no test files), `notes.txt`, `test_a.py`, `test_b.py` and `test_data.json`:

| Processes | Groups, after the shared `__init__.py` and `conftest.py` |
|---|---|
| 1 | `alpha beta notes.txt test_a.py test_b.py test_data.json` |
| 2 | `alpha beta notes.txt` · `test_a.py test_b.py test_data.json` |
| 3 | `alpha beta notes.txt` · `test_a.py` · `test_b.py test_data.json` |

A split never goes below the top level: one large directory is collected by one process however many CPUs are free, and a suite with fewer weighted entries than processes produces fewer groups.

Each process runs `pytest --collect-only -qq --assert=plain -p no:cacheprovider tests/` from its own tree, with a plugin whose `pytest_ignore_collect` hook skips every top level entry outside its group. Groups run at the same time and are joined in group order, which is name order, so the joined list matches the single process list.

### Failures

- A side fails when any of its groups fails to collect, or its process exits with an error. A side with no tests collected also fails. Either way the job fails with the collection error described in [What it compares](#what-it-compares). Every failing module and its exception is printed to the job log.
- A group that collects no tests adds nothing and does not fail the side.
- Groups never share an entry, so a test id returned by two groups means the split leaked; the side then fails instead of counting the test twice. A test id repeated within one group is kept, as a single process collection keeps it.
- The joined list is what the [collection cache](#configuration-and-ci-usage) stores and what the dev record producer writes, so a cached or recorded side is graded exactly as a collected one.

### Checking the split in CI

The collection runs inside the `Hold the base revision's test count` step of each `test-count` job. Read that step's duration across pull request runs:

```bash
gh api 'repos/the-cloud-clockwork/agentihooks/actions/runs/RUN_ID/jobs?per_page=100' --paginate \
  --jq '.jobs[] | select(.name | startswith("test-count")) | .steps[]
        | select(.name == "Hold the base revision'\''s test count")
        | {started_at, completed_at, conclusion}'
```

Replace `RUN_ID` with a Tests run id. The step's log carries the usual `head collects N tests, base floor F` line; the split prints nothing of its own. When one step is much slower than others from the same hour, check whether the base was collected as well (`no dev record matches the base, so it is collected`), which halves the processes per side. Node load also moves this step by several seconds, so compare cohorts from the same hours of day.

### Contract tests

`tests/test_count_floor.py` pins the behaviour:

| Test | Holds |
|---|---|
| `test_collection_splits_across_processes_and_matches_one_process` | Three processes return exactly the single process ids, in order, and three distinct processes ran |
| `test_collection_groups_are_contiguous_runs_weighted_by_test_files` | The grouping shown above, independent of directory listing order |
| `test_each_collected_side_shares_the_processes` | Four processes over base and head start two per side |
| `test_one_failed_group_fails_the_whole_side` | One failing group fails the side |
| `test_groups_that_overlap_fail_the_side_instead_of_inflating_the_count` | A leaked split fails closed |
| `test_a_group_that_collects_no_tests_adds_nothing` | An empty group adds nothing |
| `test_collection_processes_follow_the_cpu_quota` | The process count follows the CPU quota |

A change to the split, like any change to the gate, grades from the next pull request on, since the base revision's copy of `tests/count_floor.py` runs.

## Dev suite records

A pull request can reuse the base suite collected on protected `dev` instead of collecting that same base again. The record contains the collected test ids, preserving both the count and the lost-test admission rules. It never substitutes a dev result for the head collection or changes the floor.

The **Dev test count records** workflow runs on pushes to `dev`, independently of the Tests workflow. Its `count-record` job collects a clean detached checkout of the pushed commit and uploads `record.json` as `count-record-3.12`, retained for seven days. Separate production ensures that reusing an earlier Tests result does not skip recording the new dev commit. The job is best effort, with a six minute job timeout and a two minute recording step timeout; an absent record leaves later grading to collection.

### Which record is trusted

For a same-repository pull request into `dev`, Tests uses a CI App token with Actions read permission to look up the record. The base checkout's own `tests.count_floor.dev_record` selects from the first hundred artifacts named for the Python version. It accepts an unexpired artifact only when its run:

- names branch `dev`;
- names the exact merge base commit printed as `BASE: <sha>`;
- has matching repository and head repository ids, both present.

A newer dev commit's record does not match an older merge base. Records supplied by the pull request's own run or by a fork are ignored. The artifact selector and the floor grader run from the base checkout, so editing the head's record or grader cannot lower its own floor. The workflow fetches these records only for pull request events; other grading events use collection or the separate collection cache.

The base grader then validates the record's `identity` against the clean base checkout. Identity covers the Git tree, tests path, Python version, platform, installed package names and versions, collector source and `PYTEST_` environment options. The project's own commit-derived package version is excluded. `nodeids` must be a nonempty list of strings. A changed dependency environment can therefore cause a miss even for the correct commit.

### Configuration and CI usage

Reuse is automatic; contributors do not upload or commit records. The current producer and consumer both use Python 3.12. Maintainers changing that matrix must keep the artifact name and collection environment aligned. The consumer requires the existing `TCC_CI_CLIENT_ID` and `TCC_CI_APP_PRIVATE_KEY` secrets to mint its Actions read token.

The producer runs this command inside its clean CI checkout, with the output directory already created:

```bash
python -m tests.count_floor \
  --record "$RUNNER_TEMP/count-record/record.json" \
  --head "$RUNNER_TEMP/base"
```

The existing base grading command accepts these environment settings:

| Setting | Use |
|---|---|
| `TEST_COUNT_BASE_RECORD` | Downloaded record file. Unset, unreadable, malformed or mismatched records fall back to base collection. |
| `TEST_COUNT_COLLECTION_CACHE` | Directory for the separate exact collection cache, used for sides still needing collection. |
| `TEST_COUNT_ENVIRONMENT` | Environment key required to enable that cache; CI uses the restored test environment's primary cache key. |
| `TEST_COUNT_HEAD_COLLECTION` | Optional output file for collected head ids, reused by later CI steps. |
| `TEST_COUNT_REVIEWED_HEAD`, `TEST_COUNT_REVIEWS` | The pull request head and its reviews; see [Code owner approval](#code-owner-approval). |

For example, the workflow grades from the base checkout with the downloaded record:

```bash
cd "$RUNNER_TEMP/base"
TEST_COUNT_BASE_RECORD="$RUNNER_TEMP/base-record.json" \
  python -m tests.count_floor \
  --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
```

These are CI collection commands; collection imports the test suite. Run verification through CI under the repository's test policy.

### Inspecting a hit or miss

Find a pull request's Tests run and read its job output:

```bash
gh run list --workflow test.yml --event pull_request --limit 10
gh run view RUN_ID --log | rg 'BASE: |suite count|dev record|head collects'
gh run list --workflow count-record.yml --branch dev --limit 10
gh api 'repos/the-cloud-clockwork/agentihooks/actions/artifacts?name=count-record-3.12&per_page=100' \
  --jq '.artifacts[] | {id, expired, workflow_run}'
```

Replace `RUN_ID` with the selected run's id. Compare the artifact run's `head_sha` with the logged base, rather than the latest dev head.

| Output or symptom | Meaning and action |
|---|---|
| `base count read from the dev record` | Provenance and identity matched; only the head needs collection, subject to its own collection cache. |
| `No dev run recorded the suite count for <sha>.` | No eligible artifact appeared in the lookup. Check the producer for that exact commit, artifact expiry and the hundred-artifact lookup limit. Base collection continues. |
| `no dev record matches the base, so it is collected` | The configured file is missing, invalid or has a different identity. Check environment differences and the downloaded artifact. |
| Artifact listing, download or extraction failed | The fetch step leaves no usable record and grading falls back. Inspect that step's error before treating a slower job as a grading failure. |
| `The tree did not collect cleanly, so no record is written.` | The producer could not collect tests or its checkout identity was unavailable or changed. Fix that producer failure through a normal pull request; later grading still collects the base. |

A cache miss preserves normal grading. Token creation is a separate workflow step: its failure can fail the job before the best effort artifact fetch runs. A fallback collection error or empty suite still fails the floor as described above.

## Admitted lost tests

Admission is computed only when the head collects fewer tests than the base. Everything comes from parsing the two checkouts, except the [signed retirements](#signed-retirements) a pull request adds.

A test id in the base and not in the head is admitted when it referenced code the head deletes (a top level name, a keyword branch, or executable steps in a JSON fixture or workflow), when its parametrized id names a file the head deletes, or when the pull request retires it under a Standards reader's signature and, on a pull request, a code owner's approval.

### Deleted definitions and constants

A lost test is admitted through a definition when all of these hold:

1. **The test referenced a subject.** In the base, the test function's body (parametrize ids stripped, `Test` classes resolved) uses a name imported by its own file with `from X import n`, or an attribute of a module the file imports (`import pkg.mod` then `pkg.mod.f`, or `from pkg import mod` then `mod.f`). Calls into module level helpers in that test file are followed with cycle detection. Parameters and local bindings shadow imported names and helpers; uncalled nested function and class bodies are excluded. Decorators, implicit pytest fixture execution, relative imports and attributes of anything that is not an imported module do not establish a reference.
2. **The subject was a top level definition in the base.** In a base file outside `tests/`, at module level, including inside a module level `if` or `try` (not `try`/`except*`):

   - a function, async function or class;
   - a constant, meaning any module level name that is the target of an assignment or annotation: the name on the left of a plain assignment (`LIMIT = 3`, `log = getLogger()`, each name of `A = B = 3`) or of an annotation, with or without a value (`LIMIT: int = 3`, `LIMIT: int`). Unpacking (`A, B = ...`), augmented assignment (`LIMIT += 1`) and attribute targets do not define a constant.

   The module name comes from the file path.
3. **The head deletes it.** The subject's own module no longer binds its name. An unchanged binding of the same name in another module does not conceal that deletion. A new or changed binding of that name anywhere in the head, `tests/` included, blocks admission. Binding comparisons include the syntax of their definitions or enclosing binding statements, so moving a subject into an existing same-name definition still admits nothing. Methods, assignments, import aliases, `except ... as` names and `match` captures count as bindings.

A test that reads a constant is admitted once the head deletes that binding without introducing or replacing another binding of its name:

```python
# base pkg/limits.py
RETRY_LIMIT = 3

# base tests/test_limits.py, deleted with RETRY_LIMIT
from pkg.limits import RETRY_LIMIT

def test_retry_limit_is_three():
    assert RETRY_LIMIT == 3
```

### Executable fixtures and module entry points

A lost test may reference a JSON fixture through `Path.read_text()` or `Path.read_bytes()`, directly or through a called local helper. The path must resolve statically from imported `pathlib.Path`, `__file__`, module level assignments, names the test or helper binds exactly once by a plain assignment, parent access and literal path joins. A local name bound twice, bound by a loop, `with`, import or `except`, or passed as a parameter does not resolve. The resolved file must stay inside the base checkout and contain nonempty shell `run` strings in a `steps` list. Ordinary data, unresolved paths and paths escaping through `..` or symlinks establish no admission. The fixture must disappear at its old path and none of its executable step strings may survive in another JSON file in the head.

An explicit `runpy.run_module("pkg.mod", run_name="__main__")` resolves calls inside that module's `if __name__ == "__main__"` guard. A called function must satisfy the same deletion rules as a direct import. A dynamic module target, an ordinary module import, or a surviving entry point admits nothing.

### Parametrized ids that name a deleted file

A lost parametrized case is admitted when its id, the text between the brackets, names a file the head deletes. The id may name the file by its repository path or by its file name when no head file has that name. The name must sit on id boundaries, so `nightly-report.yml-stats` names `nightly-report.yml` and `prenightly-report.yml` does not. A name without an extension never matches. A file counts as deleted only when it is gone from its base path and no head file holds the same bytes, so a moved file keeps its cases counted. A deleted file also counts as renamed, and admits nothing, when the head adds any file with the same extension or the same name before its extension, so a file renamed with edits keeps the floor where it was wherever its case moves. In a git checkout only tracked files count, so files a job writes into the workspace neither keep nor rename anything. For a head that deletes `.github/workflows/nightly-report.yml`, the log reads:

```text
tests/test_ci_queue_seed.py::test_no_job_waits_on_the_retired_queue_baseline[nightly-report.yml-stats] is admitted: its id names .github/workflows/nightly-report.yml, which the head deletes
```

### Signed retirements

A lost test of behaviour the pull request removes from a module that survives has no deleted subject to point at. The pull request lists it instead, one JSON file per test under `test-retirements/`, named by the SHA-256 of its node id, in the layout mutation clearances use:

```json
{
  "tests/test_ci_workflow.py::test_nightly_report_uploads_its_summary": {
    "reader": "Standards reader",
    "reason": "The head removes the nightly report summary upload this test held."
  }
}
```

When the job fails, the `test-count` log names every lost test no other rule admits as `<node id> is in the base and not in the head`; retire only those whose behaviour the pull request removes. Write them with the floor itself, which refuses a ruling without a Standards reader or a reason:

```bash
python -m tests.count_floor --retire "<node id>" ... --reader "Standards reader" --reason "<why the behaviour is gone>"
```

An entry admits its lost test only when all of these hold:

1. Its file name matches its node id and holds that one entry.
2. The reader starts with the word `Standards`, in any case (`Standards reader`, `standards_reader`), and the reason is not blank.
3. The base has no file of that name, so the pull request itself retires the test.
4. On a pull request, a code owner of the retirement file approved the pull request head the run grades.

Any other file under `test-retirements/`, a symbolic link included, is named in the log and admits nothing; a `test-retirements` folder that is itself a symbolic link admits nothing, and `--retire` refuses to write through one. A lost test without an entry still counts against the floor. `/test-retirements/` is code-owned, so every retirement goes through owner review, which the floor checks on a pull request through [Code owner approval](#code-owner-approval).

```text
test-retirements/0000….json is not a new retirement signed by a Standards reader, so it admits nothing
tests/test_ci_workflow.py::test_nightly_report_uploads_its_summary is admitted: the pull request retires it, signed by Standards reader and approved by @code-owner
```

#### Code owner approval

The reader is text the pull request writes, so on a pull request the floor also needs a code owner's approval. The `Read the pull request reviews` step of `test-count` lists the pull request's reviews on the job's App token, which holds pull requests read, and writes one line per review with its `user`, `state` and `commit_id`. The floor step hands that file and the pull request head from the event payload to the base copy of the floor:

| Setting | Use |
|---|---|
| `TEST_COUNT_REVIEWED_HEAD` | The pull request head commit. While it is set, a retirement is admitted only with a code owner's approval of this commit. |
| `TEST_COUNT_REVIEWS` | The reviews file. A missing or unreadable file admits no retirement while the head is set. |

The owners of a retirement file are those the last matching rule of the base revision's `.github/CODEOWNERS` names, so a pull request cannot name its own approver. The floor reads anchored rules with a star inside one segment and a trailing slash for a folder, the grammar `tests/test_codeowners.py` holds the file to and matches with the same `tests.count_floor.code_owners`. One rule outside it (unanchored, `**`, `?`, a range, a negation, an escape or a folder without its slash), wherever it sits in the file, leaves no known owner, since GitHub might match it last; no retirement is then admitted. Each reviewer's last approval, request for changes or dismissal decides; a later comment leaves it standing, as on GitHub. An approval of an earlier head does not count, so every push after the approval needs a new one. Team owners are not matched, since a review names only its user.

```text
test-retirements/1f0f….json is not approved on the pull request head by a code owner (@code-owner), so it admits nothing
The pull request reviews could not be read, so no retirement is admitted
```

The approval can also be graded on its own, without collecting either tree:

```bash
python -m tests.count_floor --approvals --base "<base checkout>" --head "<head checkout>"
```

The approvals mode reads the same two settings. It exits 1 when the head holds a new signed retirement that no code owner approved on the reviewed head, and names each one; it exits 0 when every new signed retirement is approved, when the head holds none, and when no reviewed head is set.

```text
test-retirements/1f0f….json is not approved on the pull request head by a code owner (@code-owner), so it admits nothing
::error::tests/test_ci_workflow.py::test_nightly_report_uploads_its_summary is retired without a code owner's approval of the pull request head.
```

The mode needs `--base`; beside `--record` or `--retire` it is refused with a usage error.

A review does not start a Tests run. After a code owner approves the head, rerun the failed `test-count` job. GitHub refuses an author's approval of their own pull request, so a pull request authored by the only code owner's account cannot retire a test.

Merge groups and dispatched runs carry no pull request head, so they grade signed retirements by the first three rules. A pull request enters the queue only with its own `test-count` green, which required the approval.

### Deleted keyword branches

A lost test is also admitted when it passed a keyword to a function whose branch on that keyword the head removed, while the function itself stays. All of these hold:

1. **The test passed the keyword.** In the base test body, a call resolves to an imported function by the same rules as above and passes the parameter by name (`gate_steps(jobs, queue_coverage=True)`). A value passed by position, or through `**options`, does not count.
2. **The keyword selected a branch in the base.** The function is defined directly at module level in a base file outside `tests/` (not inside an `if` or `try`, and not a method). The keyword is one of its parameters other than `**kwargs` (a `*args` parameter's name also counts), it appears in the condition of an `if` or `elif`, a conditional expression (`a if flag else b`) or a `while` anywhere in the function, nested functions included, and the function never assigns to that name with `=`, `:=`, an augmented assignment or a loop or `with` target. Other bindings of the name inside the function, such as an import alias, a nested `def` or an `except ... as`, are not checked.
3. **The head keeps the function and drops the keyword.** The head defines a function of the same name directly at module level in the same module. Its parameters are a subset of the base parameters, it has no `**kwargs`, and it no longer accepts the keyword. A head copy that adds any parameter, a renamed one included, admits no branch, and a function moved to another module admits none either.

The subject is written `module.function(keyword)`. A simplified example:

```python
# base scripts/ci_reuse.py
def gate_steps(jobs, queue_coverage=False):
    if queue_coverage:
        return _queue_steps(jobs)
    return _steps(jobs)

# head scripts/ci_reuse.py: the branch and its keyword are gone
def gate_steps(jobs):
    return _steps(jobs)
```

A base test that calls `gate_steps(jobs, queue_coverage=True)` and is deleted in the same change is admitted through `scripts.ci_reuse.gate_steps(queue_coverage)`. A deleted function, rather than a deleted keyword, admits its tests through its name as a definition.

### Admission output

Each admitted test is printed with the subjects that admitted it:

```text
tests/test_mod.py::test_subject is admitted: the head deletes pkg.mod.subject, which it referenced
tests/test_ci_reuse.py::test_the_queue_coverage_gate_needs_the_standalone_ratchet_outside_the_queue[push] is admitted: the head deletes scripts.ci_reuse.gate_steps(queue_coverage), which it referenced
head collects 41 tests, base floor 41
```

The line reads `the head deletes` for a keyword branch too; the function named before the parentheses is still in the head. A deleted subject admits only the lost tests that referenced it. Every other lost test still counts against the floor.

### When admission is off

If any head file cannot be parsed, or contains a star import (`from x import *`), its names are unknown, so no lost test is admitted for the whole run, through a definition, a constant, a keyword branch, a fixture or a parametrized id. Only a [signed retirement](#signed-retirements) still admits one. When the head collects fewer tests than the base, the job names each such file:

```text
pkg/legacy.py hides its names in the head (unparseable or a star import), so no lost test is admitted except by a signed retirement
```

The floor then grades by count alone.

### Limits

- Names bound dynamically (`globals()`, `setattr`, `exec`, a module `__getattr__`) are not seen, so a subject kept alive that way can still admit its tests.
- An unrelated unchanged binding is tolerated; a new or changed same-name binding conservatively blocks admission, even when it serves another purpose. This applies to constants as much as to functions.
- Keyword branches are found only in `if`, `elif`, conditional expression and `while` conditions. A parameter that only drives a `match`, a comprehension filter, an `assert` or a boolean expression outside such a condition, or is only passed on to another call, is not a branch, so its tests stay red.
- The subject is matched by the module path the test imported it from. A name re-exported through a package (`from pkg import f` where `f` is defined in `pkg/impl.py`) does not match its definition, so its lost tests stay red.
- A parametrized id admits through a file renamed with edits to a new name and a new extension at once, since the head then adds no file sharing either; and a head that adds any file of the same extension, such as a new `.py` module, keeps every deleted file of that extension from admitting by id, so those cases need a signed retirement.
- Count masking is unchanged: an unrelated lost test passes when the head adds as many tests.

## Deleting dead code with its tests

Delete the covered code and its tests in the same pull request. Keep unrelated same-name bindings unchanged. To retire one branch of a function that stays, delete the branch, its keyword parameter and the tests that passed that keyword together, and add no parameter to the function in the same change. The `test-count` log names each admitted test and the deleted subject. If a lost test stays red, the log lists it:

- for a definition or constant, check the resolved import or called helper and confirm deletion from its module with no new or changed same-name binding;
- for an executable fixture, check the resolved base path and confirm that its code disappears rather than moving;
- for a keyword branch, check that the test passed the keyword by name, that the base function tested it in a branch condition and never reassigned it, and that the head copy sits in the same module with no `**kwargs` and no new parameter.

## Proof

Dev record reuse shipped in pull requests [4239](https://github.com/the-cloud-clockwork/agentihooks/pull/4239) and [4278](https://github.com/the-cloud-clockwork/agentihooks/pull/4278). Issue [4238](https://github.com/the-cloud-clockwork/agentihooks/issues/4238) records the matched cohort: green full scope pull request Tests runs from 17 October 2026 at 22:03 to 18 October at 03:00 UTC, with the same job layout, classified by the test count log line.

| Base source | Jobs | Median test count job | Median collection step |
|---|---|---|---|
| Collected | 12 | 61.5 seconds | 25.0 seconds |
| Dev record | 18 | 53.0 seconds | 18.0 seconds |

The observed hit rate was 60 percent; this comparison measures hits against fallback within that cohort, rather than guaranteeing a hit for every pull request. Record production now runs in its own workflow.

The unmerged plant [4307](https://github.com/the-cloud-clockwork/agentihooks/pull/4307) uploaded a lowered record from its own run and deleted one test. Run [39081653661](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39081653661) ignored that record, collected the base and failed with `The head collects 1 fewer tests than the base floor of 34831.`

Collection across processes shipped in pull request [4864](https://github.com/the-cloud-clockwork/agentihooks/pull/4864) (issue [4863](https://github.com/the-cloud-clockwork/agentihooks/issues/4863)). Red run [39299441156](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39299441156) carried only the contract test: the equality with one process passed and the process count failed with `assert 1 == 3`. Green run [39300792289](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39300792289) passed it and the six other split tests. The `Hold the base revision's test count` step, successful pull request `test-count` jobs from Tests runs created between 17:00 and 18:00 UTC, the same hour on each day because node load moves this step by several seconds:

| Day | Collection | Jobs | Median step |
|---|---|---|---|
| 19 October 2026 | one process | 30 | 25.0 seconds |
| 21 October 2026 | split | 12 | 13.5 seconds |
| 22 October 2026 | split | 22 | 19.0 seconds |

The hour held fewer than thirty jobs on both later days, so every job in it is counted. The step fell by 6 to 11.5 seconds, short of the 8 second median the change targeted.

Definition admission, both ways on planted branches, graded by the delivered gate as the base copy (pull request [3492](https://github.com/the-cloud-clockwork/agentihooks/pull/3492)):

| Run | Plant | Result |
|---|---|---|
| [38808368351](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38808368351) | test lost, its subject survives | `test-count` red on 3.11 and 3.12 |
| [38808372191](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38808372191) | test lost with its subject deleted | `test-count` green on 3.11 and 3.12 |

Constant and keyword branch admission (pull request [3545](https://github.com/the-cloud-clockwork/agentihooks/pull/3545)) on the dead code removal in pull request [3512](https://github.com/the-cloud-clockwork/agentihooks/pull/3512), which deleted the queue coverage branch of `scripts.ci_reuse.gate_steps` and its tests. The second run grades a later head of that pull request, so its counts differ from the first:

| Run | Gate | Base | Head | Result |
|---|---|---|---|---|
| [38833970029](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38833970029) | before 3545 | `bad534fe`, 31984 tests | `96b27de8`, 31960 tests | `test-count` red on 3.11 and 3.12: no test admitted, floor 31984 |
| [38840053037](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38840053037) | after 3545 | `43d47665`, 32063 tests | `f0b21c39`, 32039 tests | `test-count` green on 3.11 and 3.12: 28 lost tests admitted through `scripts.ci_reuse.gate_steps(queue_coverage)`, floor 32035 |

Behaviour is pinned by `tests/test_count_floor.py`.
