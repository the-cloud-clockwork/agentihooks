# Pull request mutation gate

The Mutation workflow runs beside Tests for pull requests into dev. It discovers changed Python files in `hooks` and `scripts` from the pull request merge base. Each file runs in an isolated copy with pinned mutmut 3.6.0. Pytest runs serially; matching and importing test modules provide mutmut's function coverage stats and per mutant test selection.

Run locally with the workspace Python:

```bash
~/dev/tcc-ecosystem/.venv/bin/python -m scripts.ci_mutation --base origin/dev
```

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
