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

Each checkout collects its own `tests/` with `pytest --collect-only`, in its own directory so the base imports its own sources. Both collections run in parallel.

- A collection error, or no tests collected, on either side fails the job: `Collecting the base tests failed or found none, so the floor cannot be graded.`
- The floor is the base count minus the admitted lost tests.
- The job passes when the head count is at least the floor. Otherwise it lists every lost test that was not admitted and fails: `The head collects N fewer tests than the base floor of F.`

The floor compares counts. A renamed test passes, since the new id offsets the lost one, and so does any lost test offset by an equal number of added tests.

## Admitted lost tests

Admission is computed only when the head collects fewer tests than the base. Everything comes from parsing the two checkouts; there is no list of exempt tests.

A test id in the base and not in the head is admitted when it referenced a subject the head deletes. There are two kinds of subject: a top level name (a function, class or constant) and a keyword branch of a function.

### Deleted definitions and constants

A lost test is admitted through a definition when all of these hold:

1. **The test referenced a subject.** In the base, the test function's body (parametrize ids stripped, `Test` classes resolved) uses a name imported by its own file with `from X import n`, or an attribute of a module the file imports (`import pkg.mod` then `pkg.mod.f`, or `from pkg import mod` then `mod.f`). Only the statements of the test function's body are read, so decorators on the test, fixtures, relative imports and attributes of anything that is not an imported module never count.
2. **The subject was a top level definition in the base.** In a base file outside `tests/`, at module level, including inside a module level `if` or `try` (not `try`/`except*`):

   - a function, async function or class;
   - a constant, meaning any module level name that is the target of an assignment or annotation: the name on the left of a plain assignment (`LIMIT = 3`, `log = getLogger()`, each name of `A = B = 3`) or of an annotation, with or without a value (`LIMIT: int = 3`, `LIMIT: int`). Unpacking (`A, B = ...`), augmented assignment (`LIMIT += 1`) and attribute targets do not define a constant.

   The module name comes from the file path.
3. **The head deletes it.** No Python file anywhere in the head, `tests/` included, binds the subject's name at any depth: no function, method or class of that name, no assignment target, no import alias, no `except ... as` or `match` capture. A subject moved to another module, or into `tests/`, still binds its name and admits nothing.

A test that reads a constant is admitted once the head removes every binding of it:

```python
# base pkg/limits.py
RETRY_LIMIT = 3

# base tests/test_limits.py, deleted with RETRY_LIMIT
from pkg.limits import RETRY_LIMIT

def test_retry_limit_is_three():
    assert RETRY_LIMIT == 3
```

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

If any head file cannot be parsed, or contains a star import (`from x import *`), its names are unknown, so no lost test is admitted for the whole run, through a definition, a constant or a keyword branch. When the head collects fewer tests than the base, the job names each such file:

```text
pkg/legacy.py hides its names in the head (unparseable or a star import), so no lost test is admitted
```

The floor then grades by count alone.

### Limits

- Names bound dynamically (`globals()`, `setattr`, `exec`, a module `__getattr__`) are not seen, so a subject kept alive that way can still admit its tests.
- Matching is by name in the head: an unrelated binding of the same name anywhere in the head, a local variable included, keeps the subject alive and the lost test red. This applies to constants as much as to functions.
- Keyword branches are found only in `if`, `elif`, conditional expression and `while` conditions. A parameter that only drives a `match`, a comprehension filter, an `assert` or a boolean expression outside such a condition, or is only passed on to another call, is not a branch, so its tests stay red.
- The subject is matched by the module path the test imported it from. A name re-exported through a package (`from pkg import f` where `f` is defined in `pkg/impl.py`) does not match its definition, so its lost tests stay red.
- Count masking is unchanged: an unrelated lost test passes when the head adds as many tests.

## Deleting dead code with its tests

Delete the function, class or constant and the tests that exercised it in the same pull request, and remove every other binding of its name. To retire one branch of a function that stays, delete the branch, its keyword parameter and the tests that passed that keyword together, and add no parameter to the function in the same change. The `test-count` log names each admitted test and the deleted subject. If a lost test stays red, the log lists it:

- for a definition or constant, check that the test body referenced it through an import and that no head file still binds the name;
- for a keyword branch, check that the test passed the keyword by name, that the base function tested it in a branch condition and never reassigned it, and that the head copy sits in the same module with no `**kwargs` and no new parameter.

## Proof

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
