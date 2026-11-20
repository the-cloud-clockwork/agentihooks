---
title: Tick step skill and rule
parent: Reference
nav_order: 73
---

# Tick step skill and rule

Every swarm role session carries the tick pattern rule, and the `tick-step`
skill walks an agent through adding or changing a tick step the one way the
gates accept: scaffold five artefacts with `python -m scripts.tick.new_step`,
fill the body against its declared ports, run the tick pattern, catalogue and
feature map checks, and cite the equivalence and planted fault run ids in the
pull request. Both shipped in
[pull request 9844](https://github.com/the-cloud-clockwork/agentihooks/pull/9844)
with two role skill changes: the swarm engineer loads `tick-step` first for a
tick task, and the swarm master names the scaffold's inputs in a tick task
spec.

| Piece | Source | Who receives it |
| --- | --- | --- |
| Tick pattern rule | `profiles/package/rules/tick-pattern.md` | Every rendered role persona: master, engineer, cicd, planner, qa |
| `tick-step` skill | `profiles/package/skills/tick-step/SKILL.md` | Every session with the package skills installed |
| Skill evals | `profiles/package/skills/tick-step/evals/` | Skill authors and the frontmatter tests |
| Engineer line | `swarm-engineer` skill, `## Work` | Engineer seats |
| Master line | `swarm-master` skill, task specs | Master seats |

The gates the skill drives are documented on their own pages:
[Tick pattern gate](tick-pattern-gate.md) and
[The feature and scenario catalogue](tick-engine-steps.md#the-feature-and-scenario-catalogue).

## What counts as a tick task

A tick task adds, moves or changes a tick step, or touches `scripts/tick/` or
`scripts/swarm/tick.py`. The skill also loads on the words "tick step",
"new step" or "tick task". Its argument is the step name:

```text
$tick-step reap_stale_waits
```

## The tick pattern rule

The rule renders into each role persona's `CLAUDE.md` under the marker
`<!-- rule: tick-pattern.md (rule) -->`, at priority 2. It states that a tick
task loads `tick-step` first, and that a tick step is:

1. A registered step in `scripts/tick/steps/<module>.py` declaring name,
   group, lane, needs, reads, writes, effects and feature, with a floor row in
   `budgets.FLOORS` at an interval of 60, 120 or 600 seconds.
2. A body declaring what it reads and writes, reaching other systems only
   through the ports in `effects`, with no new function in
   `scripts/swarm/tick.py`.
3. A scenario row in `tests/swarm/tick_scenarios/steps.json`, a feature test
   in `tests/tick/steps/` and a `feature-map.json` entry.
4. A catalogue entry: `python -m scripts.tick.catalogue check` holds the
   registry against the feature map and the scenario index.
5. One kind of change per pull request; a move cites the tick equivalence and
   planted fault run ids.

The rule changes no gate. Points 1 and 2 are what the
[tick pattern gate](tick-pattern-gate.md) grades; points 3 and 4 are what the
catalogue check grades; point 5 is review practice the skill asks for in the
pull request body.

## The scaffold

`python -m scripts.tick.new_step` writes the five artefacts of one step from
the repository root and prints each file it wrote:

```bash
python -m scripts.tick.new_step <name> --group <group> --lane tick|slow \
  --interval 60|120|600 --effects <port,port> --feature <feature id> --module <steps module>
```

| Option | Required | Values |
| --- | --- | --- |
| `name` | Yes | The step name; its identifier form (lower case, runs of non word characters joined by `_`) names the function and test |
| `--group` | Yes | The tick group the step runs in, for example `observe` |
| `--lane` | Yes | `tick` or `slow` |
| `--interval` | Yes | `60`, `120` or `600`, written as `CRITICAL_S`, `WIDE_S` or `SLOW_S` |
| `--effects` | No | Comma separated field names of `scripts.tick.ports.tools.Tools`: `ledger`, `coordination`, `source_control`, `runtime`, `processes`, `inbox`, `llm`, `metrics`, `host`. Empty by default |
| `--feature` | Yes | A feature map id; a new id adds an `inventory` entry |
| `--module` | Yes | A steps module name such as `observe`; never `budgets` |
| `--scenario` | No | The tick scenario the index row lists, default `running`; it must be a golden scenario |
| `--root` | No | The tree to write, default the working folder |

The five artefacts:

| Artefact | What the scaffold writes |
| --- | --- |
| Step | A `CallStep` stub `_<name>(tick, get) -> list[str]` returning `[]`, registered with empty `needs`, `reads` and `writes`, the lane, effects and feature, appended to `scripts/tick/steps/<module>.py` with any imports it lacks |
| Budget row | `"<name>": Timing(LIGHT_S, <interval name>),` at the end of `FLOORS` in `scripts/tick/steps/budgets.py`; `LIGHT_S` is a two second floor |
| Feature test | `tests/tick/steps/test_<name>.py`, marked `@pytest.mark.feature("<feature>")`, asserting the step's group, lane, feature and effects |
| Index row | `"<name>": ["<scenario>"]` under `scenarios` in `tests/swarm/tick_scenarios/steps.json` |
| Feature entry | The test node id added to the feature's `tests` list in `feature-map.json` |

A module the steps package does not hold is created with a one line
docstring and appended to `MODULES` in `scripts/tick/steps/__init__.py`. Every
Python file the scaffold writes is import sorted and formatted with ruff.

The scaffold refuses, before writing anything:

| Refusal | Cause |
| --- | --- |
| `unknown port <port>; ports are ...` | An `--effects` name that is not a `Tools` field |
| `a step needs a name, a group and a feature id` | A name with no word characters, or an empty group or feature |
| `<module> is not a steps module name` | A module name that is not a lower case identifier, or `budgets` |
| `unknown scenario <scenario>` | A `--scenario` with no golden |
| `<name> already has step, budget row, ...` | Any of the five artefacts already holds the name |
| `a step needs a name, --group, --lane, --interval, --feature and --module` | A required option is missing |

Report which artefacts a step has:

```bash
python -m scripts.tick.new_step --check <name>
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

It exits 0 only when all five are `true`. An index row counts when the name is
under `scenarios` or `unmapped`.

## Working a tick task with the skill

### 1. Scaffold

Take the name, group, lane, effects and feature from the task spec and run
the scaffold, then `--check`. A changed step keeps its artefacts: the scaffold
refuses its name, so edit the existing artefacts and run `--check` instead.
Changing only an interval means editing the floor row in `FLOORS`.

### 2. Fill the body

- The stub is a `CallStep`: a function of the tick module and a pass state
  reader, `(tick, get)`, returning the step's action lines. Fill `needs`,
  `reads` and `writes` with what the body uses.
- A step written as a `WriteStep` or a `Step` dataclass acts only through
  `ctx.tools`, the view of the ports declared in `effects`, reads the pass view
  with `ctx.view.read(domain, key)` (never `ledger.state`), finishes under
  `ctx.deadline` and returns `Result(writes)` with its appliers in the same
  module. A write on an undeclared port raises `UndeclaredEffect`; see
  [Effects in a tick pass](tick-adapters.md#effects-in-a-tick-pass).
- Add no function to `scripts/swarm/tick.py`. A body that needs one of its
  functions moves that function into the step module in the same pull
  request.
- Fill the scaffolded test with the behaviour the feature entry names and
  keep its `@pytest.mark.feature` marker.

### 3. Check locally

Lint, format and these three graders run locally; tests run only in CI.

```bash
BASE=$(wt.sh tmp --from origin/dev | tail -1)
python -m scripts.tick_pattern --base "$BASE"
python -m scripts.tick.catalogue check
python -m scripts.feature_map
wt.sh done "$BASE"
python -m scripts.ci_prepush
```

`wt.sh` is the worktree skill's script. `--base` takes the path of an
`origin/dev` checkout, never the ref. The pattern and catalogue checks must
report 0 errors, and the feature map check must report that every link
exists.

### 4. Pull request

- One kind of change per pull request: a new step, a move, or a body change.
- Red then green per seam in CI, as in any code task.
- A move cites the tick equivalence run id, green, and a planted fault run
  id, red on a small change planted inside the moved code; glue stays under
  50 statements. The equivalence run is the `Tick equivalence` workflow; see
  [The tick equivalence run](tick-pattern-gate.md#the-tick-equivalence-run).
- Paste the scaffold output and the three check outputs in the pull request
  body.

The task is done when `--check` reports five `true`, the three checks pass,
and a move's pull request body carries the two run ids.

## Example

A local run of the scaffold on `dev` at `ea2e4f6cc6`, recorded in
`profiles/package/skills/tick-step/evals/transcript.md`:

```text
$ python -m scripts.tick.new_step eval_probe --group observe --lane tick --interval 60 --effects inbox --feature swarm-tick-characterisation --module observe
scripts/tick/steps/observe.py
scripts/tick/steps/budgets.py
tests/tick/steps/test_eval_probe.py
tests/swarm/tick_scenarios/steps.json
feature-map.json
$ python -m scripts.tick_pattern --base <origin/dev checkout>
513 tick pattern offences in 8 files, 513 on the base allowlist, 0 errors
$ python -m scripts.tick.catalogue check
0 tick catalogue errors
$ python -m scripts.feature_map
Feature map: 126 features, all rule, test and live check links exist
```

The floor row written: `"eval_probe": Timing(LIGHT_S, CRITICAL_S),`. The
offence count is the allowlist total on that commit; a step the scaffold
writes adds none.

## Writing a tick task spec

The swarm master names, in a tick task's description, the step, its group,
its lane (`tick` or `slow`), its effects (the `Tools` ports it writes through)
and its feature map id, so the engineer can run the scaffold from the spec
alone:

```bash
agentihooks ledger --slug <slug> --as <name> task add <id> "Reap stale waits" --lane eng --phase <phase> --kind code \
  --description "Add tick step reap_stale_waits in group observe, lane tick, every 60 seconds, effects inbox, feature swarm-tick-characterisation, module observe. Done when new_step --check reports five true and the three checks pass." \
  --territory scripts/tick
```

## Changing the skill or the rule

- Edit `profiles/package/skills/tick-step/SKILL.md` or
  `profiles/package/rules/tick-pattern.md` through a pull request into `dev`.
  Both paths, and `profiles/package/skills/tick-step/*`, are `TICK_INPUTS`, so
  the change runs the tick equivalence workflow.
- Running sessions pick up a rule change after `agentihooks refresh-rules`;
  new sessions load it at startup.
- `tests/test_skill_frontmatter.py` holds both files:
  - the skill's name is `tick-step`, its description is at most 1024
    characters with no angle brackets and a `Use when` clause, its body is
    under 500 lines, and neither file names a manifesto, a `§` section or an
    installed skill path;
  - the skill names `python -m scripts.tick.new_step`,
    `python -m scripts.tick_pattern --base "$BASE"`,
    `BASE=$(wt.sh tmp --from origin/dev`,
    `python -m scripts.tick.catalogue check` and
    `python -m scripts.feature_map`, and asks for equivalence and planted
    fault run ids;
  - `evals/evals.json` holds at least three queries, each with expected
    behaviour, and `evals/transcript.md` shows the scaffold writing all five
    artefacts;
  - the rule has a description and an integer priority, at most 14 non blank
    body lines, names both commands and `tick-step`, and keeps its five
    numbered points with their key terms;
  - the rule renders exactly once into each of the five role personas;
  - the engineer's `## Work` section names `tick-step`, and the master's tick
    task bullet names each spec field.
- `tests/test_profile_sources.py` counts the rule among the rendered source
  rows for Claude and Codex, 13 rows each.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `<name> already has ...` from the scaffold | The step exists; edit its artefacts and run `--check` |
| `--check` reports `feature entry: false` | The feature map entry lacks the test node `tests/tick/steps/test_<name>.py::test_<name>`, often after a test rename |
| `--check` reports `index row: false` | Add the name under `scenarios` in `steps.json` with at least one golden scenario |
| `python -m scripts.tick_pattern --base origin/dev` fails to read the base | `--base` takes a checkout path; create one with `wt.sh tmp --from origin/dev` |
| `UndeclaredEffect` in a step test | The body writes through a port missing from `effects`; add it in the registration and the scaffolded test's expected set |
| The tick pattern gate reports `scripts/swarm/tick.py adds def <name>` | Move the function into the step module instead |
| A role persona lacks the rule | Render the profile again or run `agentihooks refresh-rules` |
| An engineer starts a tick task without the skill | Check the rendered `swarm-engineer` skill carries the tick task line in `## Work` |
