---
title: Test count floor
parent: Reference
nav_order: 10
---

# Test count floor

The `test-count` job in the Tests workflow fails when the head collects fewer tests than its base. It counts collected tests, so it stops a pull request from passing by deleting the tests that would fail it or keeping them from being collected. A skip marker leaves a test collected and is not counted as a loss. One exception lets a change delete dead code together with its tests: a lost test is admitted when the head deletes a top level function or class that the test referenced.

## Where it runs

`test-count` runs on Python 3.11 and 3.12 for pull requests, merge groups and dispatched runs that pass admission. Pushes do not run it. It checks out the base revision into a second worktree and runs the base revision's own `tests/count_floor.py`:

```bash
cd "$RUNNER_TEMP/base"
python -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
```

The base revision's copy grades, so a pull request cannot loosen its own floor. A base without `tests/count_floor.py` fails the job. A change to the gate therefore takes two pull requests: the first changes the tests to accept both the old and the new behaviour, the second changes the gate, which grades from the next pull request on.

The base is the merge base of the head with the event's base: `origin/dev` for pull requests into dev, the pull request base, the merge group base, or the `base` input of a dispatched run. The job prints it as `BASE: <sha>`.

## What it compares

Each checkout collects its own `tests/` with `pytest --collect-only`, in its own directory so the base imports its own sources. Both collections run in parallel.

- A collection error, or no tests collected, on either side fails the job: `Collecting the base tests failed or found none, so the floor cannot be graded.`
- The floor is the base count minus the admitted lost tests.
- The job passes when the head count is at least the floor. Otherwise it lists every lost test that was not admitted and fails: `The head collects N fewer tests than the base floor of F.`

The floor compares counts. A renamed test passes, since the new id offsets the lost one, and so does any lost test offset by an equal number of added tests.

## Admitted lost tests

Admission is computed only when the head collects fewer tests than the base. Everything comes from parsing the two checkouts; there is no list of exempt tests.

A test id in the base and not in the head is admitted when all of these hold:

1. **The test referenced a subject.** In the base, the test function's body (parametrize ids stripped, `Test` classes resolved) uses a name imported by its own file with `from X import n`, or an attribute of a module the file imports (`import pkg.mod` then `pkg.mod.f`, or `from pkg import mod` then `mod.f`). Only the statements of the test function's body are read, so decorators on the test, fixtures, relative imports and attributes of anything that is not an imported module never count.
2. **The subject was a top level definition in the base.** A function, async function or class defined at module level, including inside a module level `if` or `try` (not `try`/`except*`), in a base file outside `tests/`. The module name comes from the file path.
3. **The head deletes it.** No Python file anywhere in the head, `tests/` included, binds the subject's name at any depth: no function, method or class of that name, no assignment target, no import alias, no `except ... as` or `match` capture. A subject moved to another module, or into `tests/`, still binds its name and admits nothing.

Each admitted test is printed with the subjects that admitted it:

```text
tests/test_mod.py::test_subject is admitted: the head deletes pkg.mod.subject, which it referenced
head collects 41 tests, base floor 41
```

A deleted subject admits only the lost tests that referenced it. Every other lost test still counts against the floor.

### When admission is off

If any head file cannot be parsed, or contains a star import (`from x import *`), its names are unknown, so no lost test is admitted for the whole run. The job names each such file:

```text
pkg/legacy.py hides its names in the head (unparseable or a star import), so no lost test is admitted
```

The floor then grades by count alone.

### Limits

- Names bound dynamically (`globals()`, `setattr`, `exec`, a module `__getattr__`) are not seen, so a subject kept alive that way can still admit its tests.
- Matching is by name in the head: an unrelated binding of the same name anywhere in the head keeps the subject alive and the lost test red.
- The subject is matched by the module path the test imported it from. A name re-exported through a package (`from pkg import f` where `f` is defined in `pkg/impl.py`) does not match its definition, so its lost tests stay red.
- Count masking is unchanged: an unrelated lost test passes when the head adds as many tests.

## Deleting dead code with its tests

Delete the function or class and the tests that exercised it in the same pull request, and remove every other binding of its name. The `test-count` log names each admitted test and the deleted subject. If a lost test stays red, the log lists it; check that its body referenced the deleted definition through an import, and that no head file still binds the name.

## Proof

Both ways on planted branches, graded by the delivered gate as the base copy (pull request [3492](https://github.com/the-cloud-clockwork/agentihooks/pull/3492)):

| Run | Plant | Result |
|---|---|---|
| [38808368351](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38808368351) | test lost, its subject survives | `test-count` red on 3.11 and 3.12 |
| [38808372191](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38808372191) | test lost with its subject deleted | `test-count` green on 3.11 and 3.12 |

Behaviour is pinned by `tests/test_count_floor.py`.
