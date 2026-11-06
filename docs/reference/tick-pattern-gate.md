---
title: Tick pattern gate
parent: Reference
nav_order: 72
---

# Tick pattern gate

`python -m scripts.tick_pattern` holds every tick step to one shape. It
measures the tick code for offences against that shape and records them per
file in `tests/TICK_PATTERN_ALLOWLIST.json`. The list only shrinks: a pull
request may remove offences, never add one its base does not already hold.
The gate shipped in
[pull request 9658](https://github.com/the-cloud-clockwork/agentihooks/pull/9658);
[pull request 9705](https://github.com/the-cloud-clockwork/agentihooks/pull/9705)
narrowed grading to the files a pull request touches;
[pull request 9726](https://github.com/the-cloud-clockwork/agentihooks/pull/9726)
removed the bootstrap path, so a base without the allowlist and a dev without
the grader both fail;
[pull request 9794](https://github.com/the-cloud-clockwork/agentihooks/pull/9794)
added a second run in the `Tick equivalence` workflow for every pull request
that touches a tick input (see
[The tick equivalence run](#the-tick-equivalence-run)).

## What it measures

The graded scope is `SCOPE`: every tracked Python file under `scripts/tick/`
and the tick module `scripts/swarm/tick.py`. Files outside the scope are never
read. The gate records four kinds of offence:

| Offence | Recorded as | Where |
| --- | --- | --- |
| A top level `def`, `async def` or `class` in the tick module | `def <name>` (classes too, for example `def Claims`) | `scripts/swarm/tick.py` only |
| A registered step with no row in the budgets table | `step <name>: no budget row` | Every graded file |
| A step whose budget row declares an interval other than `CRITICAL_S`, `WIDE_S` or `SLOW_S` | `step <name>: interval outside CRITICAL_S, WIDE_S, SLOW_S` | Every graded file |
| A step that does not pass one of `lane`, `needs`, `reads`, `writes`, `effects` or `feature` | `step <name>: no <field>`, one line per missing field | Every graded file |
| An import of an adapted implementation | `import <module>` | Files under `scripts/tick/` outside `scripts/tick/adapters/` |

### Registered steps

A step is any call to `CallStep`, `WriteStep` or `register`, bare or as an
attribute (`steps.CallStep`, `registry.register`), whose first positional
argument is a string literal. That literal is the step name. A call whose
first argument is not a literal is skipped, such as `registry.register(_step)`, and
is not checked against any rule. In
`registry.register(CallStep("name", ...))` the outer call is skipped and the
inner `CallStep` is graded.

A field counts as declared when it is passed by keyword or in its positional
slot:

| Callee | Positional slots |
| --- | --- |
| `CallStep` | `name`, `group`, `call`, `needs`, `reads`, `writes`, `lane` |
| `WriteStep` | `name`, `group`, `call`, `writes`, `needs`, `reads` |
| `register` | `name` |

`scripts.tick.registry.register` itself takes a `Step`, so the `register`
rule only catches a call shaped like `register("<name>", ...)`; real steps are
graded at their `CallStep` or `WriteStep` construction.

`effects` and `feature` have no positional slot and must always be passed by
keyword. A default value does not count: a step that relies on
`CallStep`'s default `lane`, empty `needs` or empty `effects` is an offence
until it passes them explicitly, even as `frozenset()`.

### Budget rows

Budget rows come from the `MEASURED` dictionary in
`scripts/tick/steps/budgets.py`, read from the source without importing it.
Each row's interval is the second positional argument of its
`Timing(p95_s, interval_s)` call, and it must be one of the names `CRITICAL_S`,
`WIDE_S` or `SLOW_S`. A literal number or a keyword `interval_s=` is recorded
as outside the set. When the tree has no budgets file, every step has no
budget row.

### Banned imports

These modules are reached through the tick adapters
([Tick adapters](tick-adapters.md)), so step code may not import them or
anything beneath them:

```
scripts.inbox
hooks.classifier
scripts.swarm.metrics_outbox
scripts.swarm.host_budget
scripts.swarm.refresh_job
scripts.swarm.snapshot_job
```

`import a.b` records `import a.b`. `from a import b` records both
`import a` and `import a.b`, and each is checked. Relative imports are
resolved against the file's own package, so `from ...inbox import store` in
`scripts/tick/steps/one.py` records `import scripts.inbox` and
`import scripts.inbox.store`. A name that only shares a prefix, such as
`scripts.inboxes`, is not banned. The adapters folder and the tick module may
import these modules. Dynamic imports such as `importlib.import_module` or
`__import__` are not graded.

## The allowlist

`tests/TICK_PATTERN_ALLOWLIST.json` maps each graded file holding at least
one offence to its sorted, deduplicated offences. It is written as sorted
JSON with a one space indent and a trailing newline:

```json
{
 "scripts/swarm/tick.py": [
  "def Claims",
  "def Failed",
  "def Launch"
 ],
 "scripts/tick/steps/agents.py": [
  "step conversations: no effects",
  "step conversations: no feature",
  "step conversations: no lane"
 ]
}
```

At `dev` `b91a0d7c1b` it held 549 offences in 8 files; the self grade under
[Usage](#usage) prints the current totals:

| File | Offences |
| --- | --- |
| `scripts/swarm/tick.py` | 130 |
| `scripts/tick/steps/host.py` | 108 |
| `scripts/tick/steps/observe.py` | 74 |
| `scripts/tick/steps/report.py` | 74 |
| `scripts/tick/steps/tasks.py` | 49 |
| `scripts/tick/steps/masters.py` | 45 |
| `scripts/tick/steps/agents.py` | 38 |
| `scripts/tick/steps/place.py` | 31 |

The largest step offence kinds are `no effects` and `no feature`, 96 each: no
registered step passes either keyword today and each relies on the defaults
(see
[Effects in a tick pass](tick-adapters.md#effects-in-a-tick-pass)).

## Grading

The lint job of the Tests workflow runs the gate in the step
`Hold the tick step pattern`:

```bash
if [[ -f "$RUNNER_TEMP/grader/scripts/tick_pattern.py" ]]; then
  cd "$RUNNER_TEMP/grader"
  python -m scripts.tick_pattern --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
else
  echo "::error::dev carries no scripts/tick_pattern.py, so nothing trusted can grade."
  exit 1
fi
```

`$RUNNER_TEMP/base` is the merge base of the head with the event base
(`origin/dev` for a pull request into `dev`).
`$RUNNER_TEMP/grader` is the protected copy checked out at the pull request
base, so a pull request cannot loosen the grader that grades it. Only that
copy grades. When the protected copy has no `scripts/tick_pattern.py`, the
step prints the error above and fails; the head's own copy never grades
itself, so deleting or renaming the grader on `dev` turns every pull request
red until it is restored.

### Which files grade

Only touched files grade. A file is touched when its text differs between
base and head, when it was added or removed, or when its measurement differs
between base and head. The last case covers a budgets change: removing or
moving a row in `scripts/tick/steps/budgets.py` changes the measurement of
the step file that registers that step, so the step file grades even though
its text did not change.

### What fails

- The base has no `tests/TICK_PATTERN_ALLOWLIST.json`. Grading stops at this
  error: the head is still measured for the summary line, but no offence is
  checked and no other error is reported, because without a base list there
  is nothing to hold the head against. A head allowlist does not stand in for
  the missing base one.
- A touched file holds an offence that neither the base allowlist nor the
  base measurement holds for it. The base measurement counts so that a file
  may keep an offence it already had when the base allowlist missed it.
- An untouched file's head allowlist record holds an offence that neither
  the base allowlist nor the base measurement holds. Padding the record of a
  file the pull request does not touch is refused; a stale or missing record
  for an untouched file is not.
- The head has no allowlist, or a touched file's head record differs from its
  head measurement in either direction. Removing an offence therefore
  requires removing its line in the same change, which is how the list
  shrinks.

Before pull request 9726 a base without an allowlist allowed whatever the
head recorded, and a `dev` without the grader let the head's copy grade. Both
rules existed only so the gate could land with its first allowlist, and both
are gone, in the same way the
[whole ledger state gate](tick-ledger-view.md#the-whole-ledger-state-gate)
dropped its bootstrap.

### Output

One summary line, then one `::error::` line per error, exit 1 on any error:

```
2 tick pattern offences in 1 files, 1 on the base allowlist, 1 errors
::error::scripts/swarm/tick.py adds def more; the base allowlist does not hold it
```

```
1 tick pattern offences in 1 files, 0 on the base allowlist, 2 errors
::error::scripts/tick/steps/one.py adds step beat: no budget row; the base allowlist does not hold it
::error::tests/TICK_PATTERN_ALLOWLIST.json differs from the head's measurement; run python -m scripts.tick_pattern --write
```

A base without the allowlist still prints the summary, with `0 on the base
allowlist` and exactly one error:

```
1 tick pattern offences in 1 files, 0 on the base allowlist, 1 errors
::error::tests/TICK_PATTERN_ALLOWLIST.json is missing from the base
```

A graded file or budgets file that does not parse, a failed `git ls-files`,
a tree with no tracked Python files, an unreadable file or invalid allowlist
JSON prints only `::error::<reason>` and exits 1 with no summary line.

### The tick equivalence run

A pull request that touches a tick input is graded a second time inside the
`Tick equivalence` workflow, beside the catalogue checks and the scenario
replay. The run is reached through the `Writer lock` workflow, which the
Tests workflow calls as its `writer-lock` job:

1. On a `pull_request` event, the `inputs` job of `Writer lock` runs the step
   `Select tick guard inputs`. It lists the paths that changed between the
   merge base of the pull request base and its head
   (`git diff --name-only --no-renames`) and sets its `tick` output to `true`
   when any path matches an entry of `TICK_INPUTS`.
2. The `tick` job then calls `.github/workflows/tick-equivalence.yml`. With no
   match, or on any other event (merge group, push to `dev`, dispatch of
   `Writer lock`), the job is skipped.
3. A red step in `Tick equivalence` fails the `tick` job, which fails the
   `writer-lock` job. `Gate — Required` requires `writer-lock` to succeed on
   every pull request, so the pull request cannot enter the merge queue.

`TICK_INPUTS` holds these entries:

```
scripts/tick/* scripts/swarm/tick*.py scripts/swarm/scheduled_tick.py
scripts/swarm/store.py scripts/swarm/lease.py scripts/swarm/ledger_client.py scripts/swarm/health/*
tests/swarm/test_tick.py tests/swarm/test_delivery.py
tests/swarm/tick_scenarios/* tests/swarm/tick_equivalence.sh tests/swarm/test_tick_load.py
.github/workflows/tick-equivalence.yml .github/workflows/writer-lock.yml
scripts/tick_pattern.py scripts/size_limits.py tests/TICK_PATTERN_ALLOWLIST.json tests/tick/*
profiles/package/skills/tick-step/* profiles/package/rules/tick-pattern.md docs/reference/tick-step-catalogue.md
```

The last two lines are the tick pattern inputs pull request 9794 added: the
grader, `scripts/size_limits.py` (the grader reads its tracked file set
through it), the allowlist, the tick unit tests, the `tick-step` skill, the
`tick-pattern` rule and the tick step catalogue page
`docs/reference/tick-step-catalogue.md`. Each entry is a bash `[[ == ]]` pattern, so `*` also matches `/`:
`tests/tick/*` selects `tests/tick/steps/test_common.py`, and
`tests/swarm/tick_scenarios/*` selects the scenario steps index
`tests/swarm/tick_scenarios/steps.json`. A path is matched whole, so
`tests/test_tick_pattern_other.py` and `profiles/package/rules/delivery-tiers.md`
do not select the run.

`Tick equivalence` runs these steps in order, each without a condition or
`continue-on-error`, so the first red step ends the run:

| Step | Command | Grader copy |
| --- | --- | --- |
| `Refuse tick steps without a feature entry and a scenario` | `python -m scripts.tick.catalogue check` | Head |
| `Hold the unmapped tick steps to a shrink only list from the base grader copy` | `python -m scripts.tick.catalogue shrink --base … --head …` | Base, else head |
| `Hold the tick step pattern from the base grader copy` | `python -m scripts.tick_pattern --base … --head …` | Base, else head |
| `Replay tick scenarios on base and head concurrently and check the dropped action` | `bash head/tests/swarm/tick_equivalence.sh` | Head |
| `Hold the simulated tick load to its pass budget` | `tests/swarm/test_tick_load.py` | Head |

The workflow checks the pull request head out at `head/` and the pull
request base commit at `base/`. The pattern step is:

```bash
grader=head
if [[ -f base/scripts/tick_pattern.py ]]; then
  grader=base
fi
cd "$grader"
python -m scripts.tick_pattern --base "$GITHUB_WORKSPACE/base" --head "$GITHUB_WORKSPACE/head"
```

It grades with the same rules and prints the same output as the lint job
(see [Output](#output)), so a red pattern step names the offence, for example:

```
::error::scripts/swarm/tick.py adds def _planted_pattern_offence; the base allowlist does not hold it
```

Two differences from the lint job:

- `--base` is the pull request base commit (`github.event.pull_request.base.sha`),
  not the merge base. On a branch behind `dev` the two runs can therefore
  grade against different allowlists; merge `dev` into the branch when only
  one of them is red.
- When the base commit has no `scripts/tick_pattern.py`, the head's copy
  grades instead of failing, the same fallback the catalogue shrink step
  uses. The lint job stays the strict check: it fails when the protected
  copy lacks the grader.

The run does not execute the tick step scaffold's `--check` round trip; that
check is a separate follow up.

### Threshold pins

`scripts.threshold_pins` pins the gate's `SCOPE` as `covers`, so a change may
widen the graded folders but never drop one. It registers
`tests/TICK_PATTERN_ALLOWLIST.json` as a ratchet file graded by
`scripts.tick_pattern`, so the allowlist's shrink rule is this gate's own
grade.

## Usage

Print the current totals by grading a tree against itself:

```bash
python -m scripts.tick_pattern --base . --head .
```

On `dev` `b91a0d7c1b` it prints
`549 tick pattern offences in 8 files, 549 on the base allowlist, 0 errors`.
A self grade touches no file, so it never compares the record with the
measurement. On a clean tree, confirm the record matches the code with:

```bash
python -m scripts.tick_pattern --write
git diff --exit-code tests/TICK_PATTERN_ALLOWLIST.json
```

Record the head's measurement after removing offences:

```bash
python -m scripts.tick_pattern --write
```

`--head` defaults to the working folder. `--base` and `--write` are mutually
exclusive and one of them is required.

## Adding a step that passes

A new step adds no offence when it has a budget row with an interval from the
set, passes all six fields, and lives in a module that imports no banned
module. The row in `MEASURED`:

```python
"example": Timing(UNMEASURED_S, CRITICAL_S),
```

The registration:

```python
registry.register(
    CallStep(
        "example",
        TASK_PASSES,
        held(_example),
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=CallStep.lane,
        effects=frozenset(),
        feature="swarm-tick-example",
    )
)
```

The same registration without the keywords records six offences, and with an
`from scripts.inbox import store` line in its module two more
(`import scripts.inbox`, `import scripts.inbox.store`), each refused because
the base allowlist does not hold it. Reach the inbox, classifier, metrics
spool and host jobs through the port tools in `ctx.tools` instead. The
general steps for adding a step are in
[Tick engine steps](tick-engine-steps.md#adding-a-step-to-a-group).

## Removing offences

1. Fix the offence in code: pass the missing field, add or correct the
   budget row, move the import behind an adapter, or move a tick module
   function into a step module.
2. Run `python -m scripts.tick_pattern --write`.
3. Commit the code and the smaller allowlist together.

## Tests

`tests/test_tick_pattern.py` holds the gate: a fully declared step is clean;
each offence kind for `CallStep`, `WriteStep` and `register`, positional and
keyword; a new tick module `def`, a planted banned import and relative import
resolution are red; adapters and the tick module may import adapted modules;
a pull request cannot allowlist its own offence; kept and removed offences
are green; a base without an allowlist is red; files outside the scope; the
written JSON format; parse failures; a stale record for an untouched file is green
and a padded one red; a changed file keeping an offence its base held; and a
budgets change grading the step files whose measurement it moves.

The missing grader branch is workflow shell with no unit test. Pull request
9726 proved both bootstrap removals red with two plant steps on a diffcheck
branch cut from its head, which rerun the step body in the lint job of
[run 41894163480](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/41894163480):
one removed the grader copy, the other removed the allowlist from the base
tree, and each printed its error above.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `<file> adds step <name>: no effects` (or another field) | Pass the field by keyword in the registration, even when its value is the default |
| `<file> adds step <name>: no budget row` | Add the exact step name to `MEASURED` in `scripts/tick/steps/budgets.py`; a budgets change that renames or removes a row also turns the step's file red |
| `interval outside CRITICAL_S, WIDE_S, SLOW_S` | Pass the interval as the second positional argument of `Timing`, using one of the three names |
| `<file> adds import scripts...` | Use the port in `ctx.tools`; only `scripts/tick/adapters/` may import the adapted module |
| `scripts/swarm/tick.py adds def <name>` | Put new code in a step module under `scripts/tick/steps/`; the tick module only shrinks |
| `TICK_PATTERN_ALLOWLIST.json differs from the head's measurement` | Run `python -m scripts.tick_pattern --write` and commit the allowlist with the code |
| A file you did not touch is listed red | Its measurement moved, usually through a budgets row change, or the head allowlist adds an offence to its record |
| `::error::cannot parse <file>` with no summary | Fix the syntax error in that graded file |
| `TICK_PATTERN_ALLOWLIST.json is missing from the base` | The merge base lacks the allowlist: merge current `dev` into the branch, or restore the allowlist on `dev` through its own pull request first; adding it only on the head does not help |
| `dev carries no scripts/tick_pattern.py, so nothing trusted can grade.` | The protected copy at the pull request base lacks the grader; restore `scripts/tick_pattern.py` on `dev`, then close and reopen the pull request so its base names that `dev` |
