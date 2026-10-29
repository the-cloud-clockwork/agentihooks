---
title: Local test guard
parent: Reference
nav_order: 30
---

# Local test guard

The local test guard checks shell commands at `PreToolUse` and blocks local
test and mutation execution by default. Source and symbol inspection can pass
without enabling local tests. This keeps investigation available while CI owns
the test workload. The hook normalizes shell tool payloads from Claude, Codex
and Copilot before applying the guard.

## Inspect installed source and symbols

Use a direct import and an `inspect` reader, then print the result. For example,
with `mutmut` installed in the workspace environment:

```bash
~/dev/tcc-ecosystem/.venv/bin/python - <<'PY'
import inspect
import mutmut.__main__ as runner
print(inspect.getsource(runner.copy_also_copy_files))
PY
```

This prints the copier's source without calling it. A signature read also
passes when `pytest` is installed:

```bash
python3 -c 'import inspect, pytest; print(inspect.signature(pytest.main))'
```

The recognized readers are `inspect.getsource`, `getsourcelines`,
`getsourcefile`, `getfile`, `getdoc` and `signature`. Direct imports from
`inspect`, module aliases and imported symbol aliases are supported.

The exception is deliberately narrow. Runner references must occur inside the
reader's arguments, and the Python syntax must consist of imports, reader
calls and `print`, with simple names, attributes and constants. Assignments,
subscripts, function definitions, decorators, keyword arguments and other calls
do not qualify. Importing a runner alone does not qualify either.
`unittest.__main__` remains refused because importing it executes its runner.
Passing this guard does not bypass another guard or a task's code inspection
rules. Python still imports the module, so choose modules whose import behavior
you trust; this check is command classification, not execution isolation.

## Write prose that mentions runners

A Python heredoc may serialize an issue description containing names such as
`mutmut` and `subprocess` without becoming a test run. For example, this prints
a JSON issue body:

```bash
python3 - <<'PY'
import json
body = '''Mutation preparation must preserve mutmut fixtures and subprocess script inputs.'''
print(json.dumps({'body': body}))
PY
```

The delivered regression also covers writing the serialized body to a file.
Launch words in an ordinary prose string no longer make the guard search all
string contents as executable code. Identifier literals such as `'system'` and
`'__import__'`, and f-string contents, still participate in launch detection.
An actual subprocess, dynamic import, `eval`, `exec` or `runpy` launch keeps
runner strings subject to execution checks.

## Commands that remain refused

With local tests disabled, these examples are blocked:

| Command | Reason |
|---|---|
| `python3 -m pytest` | Executes tests |
| `python3 -m unittest discover` | Executes tests |
| `mutmut run` | Executes mutation testing |
| `python3 -m scripts.ci_mutation` | Executes the mutation gate |
| `python3 -c 'import pytest; pytest.main()'` | Calls a test runner |
| `python3 -c "import runpy; runpy._run_module_as_main('pytest')"` | Launches a runner through `runpy` |
| `npm test` or `node --test` | Executes JavaScript tests |

Adding a reader does not exempt execution elsewhere in the command. Both
`print(inspect.getsource(runner.cli()))` and a source read followed by
`runner.cli()` are refused: the former calls the runner before inspection;
the latter calls it afterward. Shell wrappers, later command segments and
command substitutions are checked too.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN` | Unset, treated as `false` | Only `true`, compared without case sensitivity, opts in to local test and mutation runs. Values such as `1` and `yes` leave the guard active. |

No setting is needed for the inspection or prose examples. To retain the
normal CI policy in a session launch environment:

```bash
export AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=false
```

Where the operator permits local runs, the guard's explicit opt in is:

```bash
export AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true
```

This setting only changes this guard. An operator's separate prohibition on
local tests still applies. In the Doctor swarm, tests run in CI; use the
[delivery tiers](delivery-tiers.md) and the
[mutation gate](mutation-gate.md) workflows for execution evidence.

## Troubleshooting

| Symptom | Check and action |
|---|---|
| `BLOCKED: Local test and mutation runs are disabled` | Read the entire submitted shell command, including later commands and substitutions. Push the change and use CI for any actual test or mutation run. |
| A source read is refused | Use the direct import and positional reader form above. Remove assignments, keyword arguments and extra calls from the inspection command; use a separate command for unrelated work. |
| A prose writer is refused | Distinguish quoted prose from real launch code, identifier strings and f-strings. Preserve the exact refused command for a regression report if it only writes data. |
| An inspection fails after passing the guard | Check the interpreter, installed package and requested symbol. Permission to inspect does not install dependencies or guarantee that the symbol exists. |
| A shell command with broken quoting is refused | Correct the shell syntax; commands that the shell parser cannot classify are denied with CI guidance. |

## Delivery evidence

[PR #4510](https://github.com/the-cloud-clockwork/agentihooks/pull/4510)
delivered the source inspection and prose classification change. The
[red proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39126913648)
reported five new pass cases failing and 233 cases passing. The
[green proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39127077745)
reported 267 passing cases, including retained execution refusals. These are
classification proofs; they do not run the inspected copier or test runners
from the sample command strings.
