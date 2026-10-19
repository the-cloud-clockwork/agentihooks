---
title: Publish fallback gate
parent: Reference
nav_order: 28
---

# Publish fallback gate

`agentihooks swarm <id> publish` grades the committed head before it pushes. A repository that ships its own `scripts/ci_prepush` module (`scripts/ci_prepush.py` or `scripts/ci_prepush/__main__.py`) runs that gate. Every other repository runs the **AgentiHooks touched Python fallback**, which the publish result names in its `gate` field.

The fallback grades only what the change touches:

- Lint and format findings count only on changed lines.
- Complexity and length limits apply only to functions whose logic changed since the merge base. Reformatting a file leaves its old offenders ungraded.
- Old offenders listed in the repository's `tests/SIZE_ALLOWLIST.json` pass while they do not grow. The list can only shrink.
- Ruff runs with `--ignore-noqa`, so a `# noqa` comment never hides an offender. No inline suppression is needed or honoured.

It runs no tests and reads no AgentiHooks baseline files. Tests run only in CI.

## When it runs

| Repository has | Gate that runs |
|---|---|
| `scripts/ci_prepush.py` or `scripts/ci_prepush/__main__.py` | The repository's own gate |
| Neither | `AgentiHooks touched Python fallback` |

The fallback runs with the same Python as the repository's own gate would, with `-I` (isolated mode), from the task worktree root. That Python must have `ruff` installed.

## What it grades

The base is `git merge-base origin/dev HEAD`. The graded files are the `.py` files the head adds, modifies, renames or changes type since that base and that still exist. Python scripts without the `.py` suffix are not graded. A renamed file is compared with its old path.

### Step 1: lint and format on changed lines

1. `ruff check --ignore-noqa` runs over the changed files with the repository's own ruff configuration. A finding fails the gate only when its line span overlaps a changed line. A syntax error fails it anywhere in the file.
2. `ruff format --diff` runs per changed file. The gate fails when the formatter would rewrite a changed line, and reports the first such line as `<path>:<line>: would reformat a changed line`.

Old lint findings and unformatted code on untouched lines do not block the push.

### Step 2: size and complexity on touched functions

The fallback measures each changed file at the head and at the merge base with these fixed limits. Ruff runs in `--isolated` mode here, so the repository's ruff configuration does not change them.

| Unit | Rule | Limit |
|---|---|---|
| Function | `C901` cyclomatic complexity | 12 |
| Function | `PLR0912` branches | 12 |
| Function | `PLR0913` arguments | 7 |
| Function | length in lines | 300 |
| File | length in lines | 3000 |

Function names are qualified by their enclosing classes and functions (`Reconciler.apply`, `outer.inner`). A repeated name in the same file gets a counter (`handler#2`).

**A function is touched** when its syntax tree differs from the same named function at the merge base. The comparison ignores line and column positions, so these changes leave a function untouched:

- reformatting, rewrapping or reindenting it;
- moving it within the file;
- editing comments inside it.

These changes touch it: any change to its statements, arguments, decorators, default values or docstring, and any change to a function nested inside it. Every function in a new file, and a function new to a file, is touched.

The file length limit is graded for every changed file, touched or not.

## Grading

For each graded unit that is over its limit at the head:

| Situation | Result |
|---|---|
| Over the limit at the head, under it at the merge base, not in the allowlist | Fails with the ruff message, such as `src/app.py: build PLR0913 Too many arguments in function definition (8 > 7)` |
| Over the limit at the merge base too | Passes while its value is not above the base value; fails with `<path>: <unit> grew from <allowed> to <head>` |
| Listed in the allowlist | Passes while its value is not above its cap (below); fails with the same `grew from` message, where `<allowed>` is the cap |

An untouched function is not graded, however far over a limit it is.

## Shrink only allowlist

The fallback reads `tests/SIZE_ALLOWLIST.json` **from the merge base**, never from the working tree. An entry added or raised on the branch therefore has no effect on that branch's push; it takes effect for later branches once it reaches `dev`. A repository without the file has no allowlist.

The file uses the `scripts.size_limits` format. Function entries are keyed `<path>::<qualified name>`; file entries are keyed by path:

```json
{
  "scripts/reconcile.py::reconcile_unit": {
    "C901": 25,
    "PLR0912": 25
  },
  "scripts/reconcile.py::Reconciler.apply": {
    "PLR0913": 9,
    "function-lines": 340
  },
  "scripts/reconcile.py": {
    "file-lines": 3400
  }
}
```

| Key | Grades |
|---|---|
| `C901`, `PLR0912`, `PLR0913` | The rule of the same name |
| `function-lines` | Function length |
| `file-lines` | File length |

The cap for a listed unit is:

- the smaller of the listed value and the unit's value at the merge base, when the unit is over its limit there;
- the listed value, when the unit is not over its limit at the merge base.

The base value caps only units that are over their limit at the merge base. An allowlisted offender that is over its limit at the base can therefore stay the same size or shrink, never grow, and raising its entry gives it no extra room.

## Examples

Publish from the task worktree as usual:

```bash
agentihooks swarm <swarm> publish --message "Add the toolset reconciler" -- scripts/reconcile.py
```

A passing publish names the gate in the `gate` field of its result. The gate's own output is shown only on failure. To see it on a pass, run the gate by hand from the worktree root:

```bash
python -I /path/to/agentihooks/scripts/swarm/publish_gate.py
```

```text
AgentiHooks touched Python fallback: /home/me/dev/worktrees/litellm-state/engineer-1
Graded 1 changed Python files
```

These outcomes were measured in [#4082](https://github.com/the-cloud-clockwork/agentihooks/pull/4082) on litellm-state `origin/dev` 53949d4, `scripts/reconcile.py`, a file with ten existing complexity offenders, among them `reconcile_unit` at `C901` 25:

| Change | Result |
|---|---|
| A format only rewrite of 175 lines, including `reconcile_unit` | Passes: no function's logic changed |
| A new 8 argument function carrying `# noqa: PLR0913` | Fails: `proof_wide PLR0913 ... (8 > 7)`; the suppression is ignored |
| One extra branch planted in `reconcile_unit` | Fails: `C901` and `PLR0912` grew from 25 to 26 |

## Evidence

On success the gate writes the graded `HEAD` commit to `ci_prepush` in the repository's git directory. `swarm publish` pushes only when that record names the committed head; otherwise it stops with `the committed HEAD has no passing prepush evidence`.

## Troubleshooting

A failing gate stops the publish with one error that quotes the gate output:

```text
AgentiHooks touched Python fallback for <worktree>: <python> -I <agentihooks>/scripts/swarm/publish_gate.py failed: AgentiHooks touched Python fallback: <worktree>
<finding>; Next: agentihooks swarm <id> publish ...
```

Each `<finding>` line is one of these:

| Message | Cause and fix |
|---|---|
| `<path>:<row>:<col>: <code> <message>` | A ruff finding on a line the change touches. Fix it on that line; `# noqa` does not silence step 1 either. |
| `<path>:<line>: would reformat a changed line` | Run `ruff format <path>` and publish again. |
| `<path>: <name> <rule> <message>` | A touched function is over a limit and was not over it at the base. Split or simplify it. Adding it to the allowlist on the same branch has no effect. |
| `<path>: <name> exceeds 300 lines` | A touched function is longer than 300 lines and was not at the base. Split it. |
| `<path>: file exceeds 3000 lines` | A changed file is longer than 3000 lines and was not at the base. Move code out of it. |
| `<path>: <unit> grew from <allowed> to <head>` | A touched offender, or an allowlisted one, got larger than its base value or cap. Bring it back to that value or below. |
| `ruff check exited <n>` or `ruff format exited <n>` | Ruff could not run, for example because it is missing from the gate's Python or the configuration is invalid. |
| `the committed HEAD has no passing prepush evidence` | The gate did not pass on the committed head. Read the gate output above it, fix, and repeat the same publish command. |

The full `swarm publish` contract (branch selection, staging, protected branches) is in the [swarm command reference](../pillars/swarm.md).
