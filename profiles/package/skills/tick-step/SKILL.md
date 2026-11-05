---
name: tick-step
description: Adds or changes a swarm tick step the one way the gates accept, by scaffolding its five artefacts with scripts.tick.new_step, filling the body against its declared ports, running the tick pattern, catalogue and feature map checks, and citing the equivalence and planted fault run ids in the pull request. Use when a task adds, moves or changes a tick step, touches scripts/tick or scripts/swarm/tick.py, or says "tick step", "new step" or "tick task".
argument-hint: "STEP_NAME"
---

# Tick step

A tick step is five artefacts: the registered step in a `scripts/tick/steps/<module>.py`
module, its floor row in `scripts/tick/steps/budgets.py`, its feature test in
`tests/tick/steps/test_<name>.py`, its row in `tests/swarm/tick_scenarios/steps.json`
and its `feature-map.json` entry. Code writes all five; you write only the body.

## 1. Scaffold

From the worktree root:

```bash
python -m scripts.tick.new_step <name> --group <group> --lane tick|slow \
  --interval 60|120|600 --effects <port,port> --feature <feature id> --module <steps module>
python -m scripts.tick.new_step --check <name>
```

- The task spec names the step, group, lane, effects and feature. Take them from it;
  an effect is a field name of `scripts.tick.ports.tools.Tools`.
- A module the steps package lacks is created and joins `MODULES`.
- The command refuses a name any artefact already holds. A changed step keeps
  its artefacts: edit them, then run `--check`.
- `--check` must report all five `true`.

## 2. Fill the body

- Act only through `ctx.tools`, the view of the ports the step declares in
  `effects`; a write on an undeclared port raises `UndeclaredEffect`.
- Read the pass view with `ctx.view.read(domain, key)`, never `ledger.state`.
- Finish under `ctx.deadline` and return `Result(writes)` with its appliers in the
  same module.
- Add no function to `scripts/swarm/tick.py`. A body that needs one of its
  functions moves that function into the step module in the same pull request.
- Fill the scaffolded test with the behaviour the feature entry names, keeping its
  `@pytest.mark.feature` marker.

## 3. Check locally

Lint, format and these graders run locally; tests run only in CI.

```bash
BASE=$(wt.sh tmp --from origin/dev | tail -1)
python -m scripts.tick_pattern --base "$BASE"
python -m scripts.tick.catalogue check
python -m scripts.feature_map
wt.sh done "$BASE"
```

`wt.sh` is the worktree skill's script. The plan writes the first command as
`python -m scripts.tick_pattern --base origin/dev`; `--base` takes the path of an
`origin/dev` checkout, never the ref. Each must end with 0 errors. Then run
`python -m scripts.ci_prepush`.

## 4. Pull request

- One kind of change per pull request: a new step, a move, or a body change.
- Red then green per seam in CI, as in any code task.
- A move cites the tick equivalence run id, green, and a planted fault run id,
  red on a small change planted inside the moved code; glue stays under 50
  statements.
- Paste the scaffold output and the three check outputs in the pull request body.

Done when `--check` reports five `true`, the three checks report 0 errors, and the
pull request body carries the equivalence and planted fault run ids where it moves code.
