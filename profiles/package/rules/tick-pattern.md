---
priority: 2
description: A swarm tick step is added or changed only through the tick pattern, scaffolded by code and graded by gates; a tick task loads the tick-step skill first
---

# Tick Pattern

A tick task loads the `tick-step` skill first. A tick step is:

1. A registered step in `scripts/tick/steps/<module>.py` declaring name, group, lane, needs, reads, writes, effects and feature, with a floor row in `budgets.FLOORS` at an interval of 60, 120 or 600.
2. A body acting only through `ctx.tools` under `ctx.deadline`, reading `ctx.view`, returning `Result(writes)`; no new function in `scripts/swarm/tick.py`.
3. A scenario row in `tests/swarm/tick_scenarios/steps.json`, a feature test in `tests/tick/steps/` and a `feature-map.json` entry.
4. A catalogue row in `docs/reference/tick-step-catalogue.md`, rendered from the registry.
5. One kind of change per pull request; a move cites the tick equivalence and planted fault run ids.

Scaffold with `python -m scripts.tick.new_step <name> --group --lane --interval --effects --feature --module`; grade with `python -m scripts.tick_pattern --base <origin/dev checkout>`.
