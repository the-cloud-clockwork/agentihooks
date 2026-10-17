---
title: Pull request mutation gate
parent: Reference
nav_order: 15
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

The source acceptance target is a slowest mutation shard within sixty seconds of the next slowest, preserving the graded mutant set, measured over twenty preflight runs before and twenty after. That timing comparison remains pending on the source task; the implementation and green preflight alone do not establish that target. See [Tests workflow timing](../ci-timing.md) for the surrounding CI timing context.

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
- **Forwarding in the pull request plan.** When the pull request plan reuses the branch preflight report of its exact head, or its plan finds no changed Python file and grades that empty set itself, the plan job uploads the report as `mutation-report-0`. The `mutation-stats` and `mutation` jobs then run no steps and still succeed, so Gate Required is unchanged. Measured on PR 3965: an empty change set spent 8 s in stats and 9 s in mutation in run 39019862061, against 25 s and 28 s in run 39018870323, with the same report.

Measured on dispatched proofs:

| Case | Old scope | New scope | Shard time |
|---|---|---|---|
| Branch refreshed with dev, base pinned to an older dev commit | 17 files | 203 own mutants | 39 s |
| Branch stacked on a graded branch | earlier branch's 303 mutants plus own | 13 own mutants | 23 s |

On the pull request that delivered this, the whole mutation chain finished 184 s into the Tests run: the plan, including the App token mint, took 21 s, and the stats and two shards finished by 184 s. Gate Required finished at 431 s, well inside the fifteen minute budget. Sonar and the coverage ratchet set that time, not mutation.

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
