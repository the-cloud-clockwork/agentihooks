---
title: Pull request mutation gate
parent: Reference
nav_order: 13
---

# Pull request mutation gate

The Mutation workflow runs beside Tests for pull requests into dev. It discovers changed Python files in `hooks` and `scripts` from the pull request merge base. Each file runs in an isolated copy with pinned mutmut 3.6.0. Pytest runs serially; matching and importing test modules provide mutmut's function coverage stats and per mutant test selection.

Run locally with the workspace Python:

```bash
~/dev/tcc-ecosystem/.venv/bin/python -m scripts.ci_mutation --base origin/dev
```

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
