---
title: Pull request mutation gate
parent: Reference
nav_order: 13
---

# Pull request mutation gate

The Mutation workflow runs beside Tests for pull requests into dev. It discovers changed Python files in `hooks` and `scripts` from the pull request merge base. Each file runs in an isolated copy with pinned mutmut 3.6.0. Pytest runs serially; matching and importing test modules provide mutmut's function coverage stats and per mutant test selection.

Run a focused mutation proof in CI from a pushed branch:

```bash
gh workflow run proofs.yml \
  --ref YOUR_BRANCH \
  -f proof=mutation \
  -f base=origin/dev
```

For a stacked branch, set `base` to its dependency branch. Branch pushes run Mutation preflight before the first draft pull request; wait for its current head to pass. Once a branch has an open pull request into `dev`, the push preflight skips it and the pull request checks own mutation. Tests and mutation runs execute in CI only.

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
| Plant on the change | [39099046126](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39099046126) | `plan` failed at **Run the suite speed contracts** 74 seconds after the run started; both fake Redis contracts failed; statistics and shards skipped |

On a clean head, [run 39098970804](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39098970804)
ran the contracts in about ten seconds and passed; the whole `plan` job took
47 seconds against its ten minute timeout. The contract names in these runs
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
| Shard target and limit | 240 seconds, 10 shards | Size the plan using estimated work and four workers, ensuring enough shards to place a mutant's units separately |

For example, ten covering tests of one second each form a ten-second head. Thirty more tests of twenty seconds each make a 600-second tail, split into five 120-second slices. Each unit runs the ten head tests and six tail tests on its own shard. All forty distinct tests remain covered, although the head runs five times. Unit cost is estimated as `5.6 + 1.25 * (head_seconds + slice_seconds)`, or 168.1 worker seconds here. A split mutant's total estimated cost includes every repeated head.

Repeated measurements use the maximum observed duration; a missing duration uses at least 5.6 seconds or the largest observed test duration, whichever is greater. A test stays whole, so a single slow test or the ten-slice cap can exceed the slice target. Traced duration and estimated worker seconds are planning inputs, not a wall-time deadline or a guarantee that jobs finish together.

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
  - every pushed commit inside the head whose own push Mutation preflight had a successful `mutation` job.
- **Which lines count.** A line is graded only when it differs from every real base. Because the given base always counts, the scope never grows past `base...head`.
- **Proof of grading.** The plan reads the preflight proof through the Actions API on the App token. Dispatched runs and pull request gates never count, since they choose their own base or may skip mutation.
- **Weakened tests.** The narrowing never hides weakened tests. If the head changed `tests/` against a newer base beyond adding tests, every line that base excluded is graded again. Adding tests means new `test_` functions, or new `Test` classes holding only test methods, with only `pytest.mark` decorators, or imports in a new test file, and no reused or repeated name. An import added to an existing test file counts as a change, since it can shadow a fixture or builtin those tests use. Anything else counts as a change: an edit, a deletion, a rename, a skip marker, a fixture, a helper, a conftest or a data file.
- **One list for every job.** The plan writes the resolved list as `bases`. The stats parts, shards, push preflight and the proofs mutation take it with `--bases`, so every job grades the same mutants.

- **Reuse on the push preflight.** Before the mutation job takes a mutation lane, which the proofs workflow and other dispatches never wait on, the cheap decision job reads the newest graded push preflight for the same head on any branch, then the branch's newest. When that run concluded success with a complete green report and this head equals it or only adds tests since, as defined above, its report is uploaded as this run's `mutation-preflight-report` with a `reuse.json` naming the source run, and the mutation job is skipped. A newer red or failed run, a missing report, any change outside `tests/` or any lookup error runs the mutation job as before, so no head goes green without a complete green report. A reused head never counts as a graded base. Measured on PR 2624: the full preflight 38151448161 took 116 s; the tests only head after it reused that report in run 38151636224, whose decision job took 18 s.
- **Forwarding in the pull request plan.** When the pull request plan reuses the branch preflight report of its exact head, or its plan finds no changed Python file and grades that empty set itself, the plan job uploads the report as `mutation-report-0`. The `mutation-stats` and `mutation` jobs then run none of their own steps and still succeed, so Gate Required is unchanged. Measured on PR 3965: an empty change set spent 8 s in stats and 9 s in mutation in run 39019862061, against 25 s and 28 s in run 39018870323, with the same report. [Reused and empty pull request runs](#reused-and-empty-pull-request-runs) covers the conditions, what a forwarded run looks like and how to inspect one.

Measured on dispatched proofs:

| Case | Old scope | New scope | Shard time |
|---|---|---|---|
| Branch refreshed with dev, base pinned to an older dev commit | 17 files | 203 own mutants | 39 s |
| Branch stacked on a graded branch | earlier branch's 303 mutants plus own | 13 own mutants | 23 s |

On the pull request that delivered this, the whole mutation chain finished 184 s into the Tests run: the plan, including the App token mint, took 21 s, and the stats and two shards finished by 184 s. Gate Required finished at 431 s, well inside the fifteen minute budget. Sonar and the coverage ratchet set that time, not mutation.

## Reused and empty pull request runs

On a pull request into dev and on a dispatched Tests run, mutation runs as three jobs of the Tests workflow: `mutation-plan`, then the `mutation-stats` matrix, then the `mutation` shard matrix. All three are needs of Gate Required. Two cases need no mutant work, so the plan job forwards the final report, the preflight's for an exact head reuse and its own grading of an empty set otherwise, and the other two jobs succeed after their runner setup steps. Exact head reuse applies only to pull requests; the empty change set path also applies to dispatched runs.

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

### What a forwarded run shows

| Job | Forwarded run | Normal run |
|---|---|---|
| `mutation-plan` | Ends with `Forward the reused or empty mutation report`; a reused run skips installing dependencies and planning | Reuse lookup and planning; the forward step is skipped |
| `mutation-stats (0)` | Succeeds after its runner setup and post steps; no `mutation-stats-*` artifact | Collects one stats part per matrix entry and uploads `mutation-stats-<part>` |
| `mutation (0)` | Succeeds after its runner setup and post steps; uploads nothing | Mutates its shard and uploads `mutation-report-<shard>` |
| Gate Required | Reads the same three job results | Reads the same three job results |

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
