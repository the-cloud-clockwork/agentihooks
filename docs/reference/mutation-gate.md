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
- **Weakened tests.** The narrowing never hides weakened tests. If the head changed `tests/` against a newer base beyond adding tests, every line that base excluded is graded again. Adding tests means new `test_` functions, or new `Test` classes holding only test methods, with only `pytest.mark` decorators, or new imports, and no reused or repeated name. Anything else counts as a change: an edit, a deletion, a rename, a skip marker, a fixture, a helper, a conftest or a data file.
- **One list for every job.** The plan writes the resolved list as `bases`. The stats parts, shards, push preflight and the proofs mutation take it with `--bases`, so every job grades the same mutants.

Measured on dispatched proofs:

| Case | Old scope | New scope | Shard time |
|---|---|---|---|
| Branch refreshed with dev, base pinned to an older dev commit | 17 files | 203 own mutants | 39 s |
| Branch stacked on a graded branch | earlier branch's 303 mutants plus own | 13 own mutants | 23 s |

On the pull request that delivered this, the whole mutation chain finished 184 s into the Tests run: the plan, including the App token mint, took 21 s, and the stats and two shards finished by 184 s. Gate Required finished at 431 s, well inside the fifteen minute budget. Sonar and the coverage ratchet set that time, not mutation.

`--head`, `--output` and `--budget` override the comparison head, evidence directory and total seconds. The default budget is eighteen minutes, leaving two minutes for setup and evidence upload in the twenty minute job. Over budget files and failed runs are named and fail the gate. All outcomes, including survivors on untouched lines, are recorded in `report.json`; CI uploads the complete evidence directory on success or failure.

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
