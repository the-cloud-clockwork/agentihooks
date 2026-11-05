---
title: Tick step scaffold
parent: Reference
nav_order: 73
---

# Tick step scaffold

`python -m scripts.tick.new_step` adds a tick step in one command. It writes
the step stub and the four artefacts the tick gates hold every step to, so a
scaffolded step passes the [tick pattern gate](tick-pattern-gate.md), the
scenario catalogue check and the [feature map](feature-map.md) validation
before any behaviour is written. `--check` reports which of the five artefacts
a step already has. The scaffold shipped in
[pull request 9788](https://github.com/the-cloud-clockwork/agentihooks/pull/9788).

## The five artefacts

| Artefact | Where | What the scaffold writes |
| --- | --- | --- |
| Step | `scripts/tick/steps/<module>.py` | A step function `_<slug>(tick, get) -> list[str]` returning no actions, and its `registry.register(CallStep(...))` with every field the tick pattern gate requires |
| Budget row | `MEASURED` in `scripts/tick/steps/budgets.py` | `"<name>": Timing(UNMEASURED_S, <interval>)` as the last row of the table |
| Test | `tests/tick/steps/test_<slug>.py` | `test_<slug>`, marked `@pytest.mark.feature("<feature>")`, asserting the step's group, lane, feature and effects |
| Index row | `scenarios` in `tests/swarm/tick_scenarios/steps.json` | `"<name>": ["<scenario>"]`, the scenarios sorted by step name |
| Feature entry | `feature-map.json` | The test node id added to the entry with the feature id, or a new `inventory` entry when no entry has that id |

`<slug>` is the step name lower cased with every run of characters other than
letters, digits and underscores turned into one underscore: step
`probe widgets` gets `_probe_widgets`, `tests/tick/steps/test_probe_widgets.py`
and the test node
`tests/tick/steps/test_probe_widgets.py::test_probe_widgets`.

### The steps module and `MODULES`

`scripts.tick.steps.MODULES` lists the step modules. Importing the
`scripts.tick.steps` package hands each one to `registry.require`, so the
registry imports them before it is next read and every listed step is
registered. When `--module` names a module the package does not hold yet, the
scaffold creates it with the header `"""The <module> steps of the tick."""` and
appends `scripts.tick.steps.<module>` to `MODULES`. A module that already
exists gains the stub at its end, with any of the imports the stub needs that
it lacks (`Callable`, `Any`, `registry`, `Lane`, `CallStep`) inserted after its
last top level import.

### Formatting

Every Python file the scaffold writes is sorted for imports and formatted with
`ruff check --select I --fix` then `ruff format`, run through the same Python
interpreter as the scaffold, so ruff must be installed in it. JSON files are
written as the gates read them: the scenario index with a one space indent,
the feature map with a two space indent and non ASCII characters kept, each
with a trailing newline.

## Usage

```bash
python -m scripts.tick.new_step "<name>" \
  --group <group> \
  --lane tick|slow \
  --interval 60|120|600 \
  --effects <port>,<port> \
  --feature <feature id> \
  --module <steps module> \
  [--scenario <scenario>] \
  [--root <tree>]
```

| Option | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | The step name, the registry key and the budgets row key. Quote a name with spaces |
| `--group` | Yes | The group the step runs in, passed as `CallStep`'s second argument |
| `--lane` | Yes | `tick` or `slow`, written as `lane=Lane("<lane>")`; see [Lanes](tick-engine-steps.md#lanes) |
| `--interval` | Yes | `60`, `120` or `600` seconds, written into the budget row as `CRITICAL_S`, `WIDE_S` or `SLOW_S` |
| `--effects` | No | Comma separated port names the step may write through, written as `effects=frozenset({...})`; empty gives `frozenset()`. The ports are the fields of `scripts.tick.ports.tools.Tools`: `coordination`, `host`, `inbox`, `ledger`, `llm`, `metrics`, `processes`, `runtime`, `source_control`. See [Effects in a tick pass](tick-adapters.md#effects-in-a-tick-pass) |
| `--feature` | Yes | The feature id, written as `feature=` on the step, on the test marker and in the feature map |
| `--module` | Yes | The module under `scripts/tick/steps/` the step goes into, without `.py` |
| `--scenario` | No | The recorded tick scenario the index lists the step under. Default `running`. It must be a key of `tests/swarm/tick_scenarios/goldens.json` without its entry point prefix |
| `--root` | No | The tree to write into. Default the working folder |

On success the command prints each file it wrote, relative to the root, in the
order step module, budgets, test, index, feature map, then
`scripts/tick/steps/__init__.py` when it created a module, and exits 0.

### Example

```bash
python -m scripts.tick.new_step "probe widgets" --group widgets --lane tick \
  --interval 120 --effects ledger,inbox --feature tick-step-widgets --module widgets
```

```
scripts/tick/steps/widgets.py
scripts/tick/steps/budgets.py
tests/tick/steps/test_probe_widgets.py
tests/swarm/tick_scenarios/steps.json
feature-map.json
scripts/tick/steps/__init__.py
```

The new module `scripts/tick/steps/widgets.py`:

```python
"""The widgets steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _probe_widgets(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the probe widgets step, given the tick module and a reader of the pass state."""
    return []


registry.register(
    CallStep(
        "probe widgets",
        "widgets",
        _probe_widgets,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"inbox", "ledger"}),
        feature="tick-step-widgets",
    )
)
```

The budget row, `MODULES` entry and index row it added:

```python
    "probe widgets": Timing(UNMEASURED_S, WIDE_S),
```

```python
    "scripts.tick.steps.widgets",
```

```json
 "scenarios": {
  "probe widgets": [
   "running"
  ]
 },
```

The test `tests/tick/steps/test_probe_widgets.py`:

```python
"""The probe widgets tick step registers with its group, lane, feature and ports."""

from importlib import import_module

import pytest

from scripts.tick import registry


@pytest.mark.feature("tick-step-widgets")
def test_probe_widgets() -> None:
    """The probe widgets step registers in group widgets on the tick lane with its feature and ports."""
    import_module("scripts.tick.steps.widgets")
    found = {step.name: step for step in registry.steps()}["probe widgets"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "widgets",
        "tick",
        "tick-step-widgets",
        frozenset({"inbox", "ledger"}),
    )
```

With no entry for `tick-step-widgets` in the feature map, the scaffold appends:

```json
{
  "id": "tick-step-widgets",
  "origin": "inventory",
  "behaviour": "The tick step probe widgets runs in group widgets on the tick lane every 120 seconds.",
  "seam": "scripts.tick.steps.widgets._probe_widgets registered as tick step probe widgets",
  "tests": [
    "tests/tick/steps/test_probe_widgets.py::test_probe_widgets"
  ]
}
```

A second step with the same `--module` appends to that module and leaves
`MODULES` alone; one with the same `--feature` adds its test node to the
existing entry's `tests`.

### After scaffolding

The stub returns no actions and declares empty `needs`, `reads` and `writes`.
Write the step's behaviour in the function, read the pass state with `get`,
and declare the keys it needs, reads and writes, following
[Adding a step to a group](tick-engine-steps.md#adding-a-step-to-a-group).
Replace `UNMEASURED_S` with the step's measured p95 once the
[tick report](tick-report.md) has timed it. Rewrite the generated `inventory`
behaviour and seam lines of a new feature entry into the behaviour the step
delivers, and extend the test to prove it. Commit every file the scaffold
printed.

## Checking a step's artefacts

```bash
python -m scripts.tick.new_step --check "probe widgets"
```

```json
{
 "step": true,
 "budget row": true,
 "test": true,
 "index row": true,
 "feature entry": true
}
```

The command exits 0 when all five are present and 1 otherwise; a name no
artefact holds prints all five `false`. Each artefact counts as present when:

| Artefact | Present when |
| --- | --- |
| `step` | A file `scripts/tick/steps/*.py` calls `CallStep`, `WriteStep` or `register` with the name as its first, literal argument |
| `budget row` | `MEASURED` in the budgets file holds the name |
| `test` | `tests/tick/steps/test_<slug>.py` exists |
| `index row` | The scenario index lists the name under `scenarios` or `unmapped` |
| `feature entry` | Some feature map entry lists the test node id `tests/tick/steps/test_<slug>.py::test_<slug>` |

`--check` takes `--root` too and ignores every other option.

## Refusals

The scaffold checks everything before it writes, so a refused run changes no
file. Each refusal prints its reason and exits 1, except missing options,
which argparse reports with usage and exit 2.

| Output | Cause |
| --- | --- |
| `<name> already has step, budget row, test, index row, feature entry` | The name holds at least one artefact; the line lists the ones found. Pick another name, or finish the missing artefacts by hand after reading `--check` |
| `unknown port disk; ports are coordination, host, inbox, ledger, llm, metrics, processes, runtime, source_control` | `--effects` names a port that is not a field of `Tools` |
| `a step needs a name, a group and a feature id` | The name has no letter, digit or underscore, so its slug is empty |
| `<module> is not a steps module name` | `--module` is not a lower case Python identifier, or is `budgets` |
| `unknown scenario <scenario>` | `--scenario` names no recorded golden scenario |
| `error: a step needs a name, --group, --lane, --interval, --feature and --module` | A required option is missing (exit 2) |
| `argument --lane: invalid choice` or `argument --interval: invalid choice` | The lane is not `tick` or `slow`, or the interval is not 60, 120 or 600 (exit 2) |
| `scripts/tick/steps/budgets.py holds no MEASURED table` | The budgets file has no `MEASURED` assignment to append to |

## Limits

- Only `CallStep` steps are scaffolded. A `WriteStep` that returns guarded
  writes is written by hand; see
  [Tick engine steps](tick-engine-steps.md).
- The interval is one of the three declared intervals; the tick pattern gate
  refuses any other.
- The step lands at the end of its module and the row at the end of
  `MEASURED`. `Registry.steps` keeps registration order, so move the
  registration when the step must run at a particular place in its group.
- A name that already holds some artefacts is refused whole; the scaffold
  never completes a partial step.

## Tests

`tests/tick/test_new_step.py` runs the scaffold in a temporary copy of the
files it edits: a scaffolded step passes the tick pattern measurement, the
feature map validation and the catalogue check beside every registered step,
and the catalogue goes red once its index row is gone; `--check` goes red on
each planted missing artefact; a new module joins `MODULES` once; every
listed module loads into the registry; a second step appends to the existing
module and a mapped feature gains the new test; an existing name, an unknown
port and missing lane or interval are refused; the test expectations and the
index are written in their canonical form; and the scaffold runs in the
current folder with no ports.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `No module named ruff` or a `CalledProcessError` naming ruff | Run the scaffold with the workspace venv Python, which has ruff installed |
| The new step is missing from `registry.steps()` | `scripts.tick.steps.MODULES` must list its module; a module created outside the scaffold is added there by hand |
| The catalogue check reports `Step <name>: no scenario lists it` | The index row was lost, often in a merge of `tests/swarm/tick_scenarios/steps.json`; run `--check` and add the row back |
| The feature map validation reports a missing behaviour test | The test file was renamed or removed; its node id must match the feature entry |
| The tick pattern gate refuses the new step | Run `--check`; a step written or edited by hand may have dropped a field or the budget row |
