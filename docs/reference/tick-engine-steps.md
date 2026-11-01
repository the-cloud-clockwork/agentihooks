---
title: Tick engine steps
parent: Reference
nav_order: 67
---

# Tick engine steps

The swarm tick (`scripts.swarm.tick.tick`) runs most of its work as registered
engine steps. Each step names a tick group and an order inside it; the tick
pass runs a group at the place where its hand written calls used to stand, so
one pass runs every call in the same order as before the move. This page covers
the engine, the step type the tick uses, and the task, planning and placement
steps that moved out of the hand written pass.

## The engine

| Part | Module | What it does |
| --- | --- | --- |
| Registry | `scripts.tick.registry` | Holds every registered step. `waves()` orders them by `order` and `needs` and refuses duplicate names, unknown dependencies, dependency cycles, a budget that is not a finite positive number, a negative interval, and two steps in one lane that write the same domain with no dependency between them |
| Group run | `scripts.tick.engine.run_group` | Runs the steps of one group sorted by `order`. Each step runs in a daemon thread under a copied context and a child deadline of its own budget; the engine waits at most that budget. A step that raises is recorded `failed`, one that runs past its budget is recorded `late`, and neither stops the next step |
| Tick site | `scripts.swarm.tick.run_group` | Runs one group with the pass state as keyword arguments and returns the actions the steps wrote, in step order. A group gets a 600 second deadline (`GROUP_BUDGET_S`) |
| Call step | `scripts.tick.steps.CallStep` | A step that runs one call of the tick module. It reads the pass state through the view and writes the actions it returned to its own domain, `actions:<step name>` |

Every `CallStep` runs in lane `tick` with a declared budget and interval from
`scripts.tick.steps.budgets.timing_of(name)`. Dependencies default to empty;
spawn declares the prerequisites described below. Its constructor takes the step name, group, order and the
call, `call(tick_module, get)`, where `get(key)` reads one value of the pass
state. The call looks up the tick module's functions at run time, so tests
that patch a function on `scripts.swarm.tick` still reach it.

## Measured budgets and declared intervals

The timing table `scripts.tick.steps.budgets.MEASURED` records each step's
p95 from a tick report over sixty passes and its interval from the plan table.
The budget is `max(1, ceil(p95_s * 1.5))` seconds: fifty percent headroom,
rounded up to a whole second. These are checked in declarations, not settings
that update automatically when new reports arrive.

| Step example | Measured p95 | Budget | Declared interval |
| --- | --- | --- | --- |
| `reap` | 14.511 s | 22 s | 60 s |
| `preempt` | 11.749 s | 18 s | 60 s |
| `spawn` | 21.812 s | 33 s | 60 s |
| `pane evidence` | 1.047 s | 2 s | 120 s |
| `metrics record` | 18.905 s | 29 s | 600 s |

`CRITICAL_S`, `WIDE_S` and `SLOW_S` declare intervals of 60, 120 and 600
seconds. This change declares the intervals; the sequential group runner
still runs each group at its existing site and does not skip a step merely
because its interval has not elapsed. Interval scheduling belongs to the
later cadence executor.

A step with no report row uses `UNMEASURED_S`, a ten second p95, giving a
fifteen second budget. It still needs an explicit timing table entry. An
unknown name raises `ValueError: Undeclared step timing: <name>` when
`CallStep` is constructed, before registration.

### Inspect and change timing

Read the live measurements without launching an extra tick:

```bash
agentihooks swarm rig-grade-swarm tick-report --passes 60
```

The report shows per step count, median, p95 and CPU share, plus pass p95 and
idle gaps. Retention can leave fewer than sixty complete passes; inspect the
counts before treating a row as a new sixty pass baseline. See
[Tick load budget](tick-load-budget.md#feature-map-entry-and-live-check) for
the output format and the separate simulated whole pass budget.

Inspect the checked in declaration from the repository root:

```bash
python -c 'from scripts.tick.steps.budgets import timing_of; print(timing_of("reap"))'
```

The result is `(22.0, 60.0)`: budget first, interval second. To change a step's
timing, update its `Timing(p95_s, interval_s)` entry in `MEASURED` through the
normal code review and CI workflow. For a newly registered `example` step
without a report row, declare it in that table before constructing it:

```python
"example": Timing(UNMEASURED_S, CRITICAL_S),
```

That is a dictionary entry using the table's existing imports and constants.
Once measured, replace `UNMEASURED_S` with its observed p95, keeping the
interval required by the plan. There is no CLI or environment override for
this table in this delivery.

### When a step exceeds its budget

The engine waits on the worker thread for at most `budget_s`. If the worker
has not returned, its `StepRecord` has `abandoned=True`, outcome `late`, the
elapsed wall time and no result. The group continues to the next step. Thus
a call that would take 300 seconds consumes only its declared waiting budget
before the next step can start; it does not occupy the group for 300 seconds.
A completed result within budget is retained. An error is recorded `failed`;
`Exception` and `SystemExit` are captured, while other base exceptions returned
by the worker are raised to the caller.

Abandoning a worker does not cancel it. Its daemon thread keeps running and
its eventual result is dropped. External writes already performed are not
rolled back. This delivery allows the next pass to start the same step while
the earlier worker remains active. Do not interpret `late` as proof that the
underlying call stopped or that retrying it cannot overlap.

### Spawn prerequisites and the forty second bound

Spawn declares only these prerequisites:

- The judge steps: `verify`, `reap`, `launch checks`, `track outages`,
  `overrun pass` and `quiet pass`.
- `orphans` and `preempt`.

`registry.latest_start("spawn", steps)` follows the longest prerequisite
path and sums the declared budgets on that path. The delivered registry
computes 22 seconds, below `SPAWN_START_S` of 40 seconds. This bounds when
spawn can start once execution follows prerequisites and each prerequisite
finishes or is left behind by its budget; it does not bound spawn completion.
Spawn itself has a 33 second budget.

The current group runner retains the group and step order shown below.
Unrelated earlier groups can still add waiting time, so forty seconds is a
prerequisite graph bound, not a measured wall time guarantee for the legacy
pass. The eager executor is required to realize that bound across groups.
The existing spawn conditions and placing lock continue to apply.

## Pass state

The tick builds one state dictionary at the start of the pass and adds the
ledger values after the `observe` group reads them. Every group site receives it:

| Key | Value |
| --- | --- |
| `slug`, `store`, `ledger`, `runtime` | The swarm and its collaborators |
| `rows` | The ledger tasks keyed by id, from the `observe` group |
| `doc` | The ledger state, from the `observe` group |
| `now_ms` | The pass time |
| `due` | The Redis key of the swarm's `spawn-due` marker, added after the `observe` group |
| `box` | The exception holder described under [Exceptions](#exceptions). The master groups (`masters`, `master seats`, `dispatch seat`, `master watch`) run through `masters.run_site`, which gives each site a fresh holder of its own |
| `config` | The swarm config, passed at each site because a pass can change it |
| `sleeping` | Passed only at the acks site |

## Groups and their sites

The groups run in this order in one pass. Groups that hold the task, planning
and placement steps are in bold; the others hold steps from other changes.

| Order | Group | Site in the pass | Steps |
| --- | --- | --- | --- |
| 1 | `start` | First line of the pass | config load, stopped branch |
| 2 | `observe` | After `start`, unless the stopped branch ended the pass | pane evidence through verify, including **spawn due** at order 70 |
| 3 | `agents` | After the observation sweeps | reap, recover outages, strays, failed panes |
| 4 | `masters` | After the agents | master wake, retire idle master, master stall, prompt stall |
| 5 | `tasks` | After the drained swarm check | orphans |
| 6 | **`task passes`** | After the fleet red notice pass | run window, verification notices, size pass, grouping release, dispatcher group |
| 7 | `place` | First line inside the placing lock | |
| 8 | **`placement acks`** | After the time left refresh, inside the placing lock | collect acks, codex hook order |
| 9 | `master seats` | Inside the placing lock, only when the swarm is not sleeping | master down, tick master, master scale, seat alarm |
| 10 | **`placement`** | After the master seats, inside the placing lock, only when the swarm is not sleeping | rank pass, bottleneck remedy, preempt, spawn |
| 11 | `dispatch seat` | Last site inside the placing lock | dispatch seat |
| 12 | `master watch` | After the placing lock | night watch, master alarm |
| 13 | `close` | Last line of the pass | conversations, session models, handoff transfers, **settle**, **close space** |

The group order and the context each site hands its steps are covered in
[Tick adapters](tick-adapters.md#empty-registry-integration).

## The moved steps

Each step makes the same call with the same arguments, through `skip_refused`,
as the hand written line it replaced. The call looks the module up on the tick
module (`tick.claim_hold`, `tick.bottleneck_remedy`, `tick.panic`), so a test
that patches one of those names on `scripts.swarm.tick` reaches the step. Where the old line carried a condition,
the step checks it and returns no actions when it does not hold.

### Task passes (`scripts.tick.steps.tasks`)

| Order | Step | Call |
| --- | --- | --- |
| 10 | run window | `run_window.step` |
| 20 | verification notices | `claim_hold.verification_notices` |
| 30 | size pass | `difficulty.size_pass` |
| 40 | grouping release | `grouping.release_pass` |
| 50 | dispatcher group | `dispatcher.group` |

### Placement (`scripts.tick.steps.place`)

| Group | Order | Step | Call and condition |
| --- | --- | --- | --- |
| `placement acks` | 10 | collect acks | `_collect_acks`, every pass |
| `placement acks` | 20 | codex hook order | `_codex_hook_order`, skipped while the swarm sleeps |
| `placement` | 10 | rank pass | `dispatcher.rank_pass` |
| `placement` | 20 | bottleneck remedy | `bottleneck_remedy.step` |
| `placement` | 30 | preempt | `preempt.run` with the seats view, only when the swarm is `running`, not on the events engine and no panic is set |
| `placement` | 40 | spawn | `_spawn` under the placement pass, only when the swarm is `running`, `readiness.tick_places` allows the engine to place and no panic is set. After it spawns, it deletes the `spawn-due` marker |

The `spawn-due` marker pairs the spawn step with the `spawn due` step of the
`observe` group (`scripts.tick.steps.observe`, calling `tick.step_spawn_due`).
When the swarm is `running`, not on the events engine and not sleeping, the
spawn due step runs `_spawn` under the `PLACING` lock and the placement pass if
the marker exists, then sets the marker to the pass time. The spawn step
deletes it after it spawns. A marker still present at the next pass means the
previous pass never reached its spawn, so the spawn due step places first,
before the agents and task groups can use up the pass.

### Close (`scripts.tick.steps.tasks`)

| Order | Step | Call |
| --- | --- | --- |
| 200 | settle | `_settle` |
| 210 | close space | `_close_space` |

The agent steps in `close` (orders 100 to 120) run before them. A stopped swarm
that is not woken ends the pass in the `stopped branch` step of the `start`
group, which makes its own `_close_space` call, and never reaches the `close`
group.

## Exceptions

The engine records a step that raises and goes on to the next one. The hand
written pass instead let the exception leave the tick. The moved steps keep the
old behaviour through the `box` in the pass state:

- Each moved step is wrapped with `tasks.held`. When its call raises any
  `Exception` or `SystemExit`, the wrapper stores the exception in `box` under
  `raised` and returns no actions.
- Every later wrapped step sees the stored exception and returns no actions
  without running its call.
- After each group site with moved steps, the tick calls `tasks.raise_held(box)`,
  which raises the stored exception from that site.

So a step that raises stops the rest of its group's moved steps, and the
exception leaves the pass at the same place the hand written call raised it.
`skip_refused` still turns a refused ledger write and a Redis timeout into no
actions, as before; only what it lets through reaches the box. The agent steps
in `close` are not wrapped, so they always run, and an error there is recorded
by the engine and does not leave the pass.

## Adding a step to a group

1. Write the call as a function `(tick, get) -> actions` in a module under
   `scripts/tick/steps/`, reading its arguments with `get`.
2. Add the step name to `budgets.MEASURED` with its measured p95 (or
   `UNMEASURED_S`) and plan interval, then register `CallStep("<name>", <group>, <order>, held(<call>))`, choosing an
   order that puts it where its hand written line stood. Use `held` when an
   exception from the call must still leave the pass.
3. Import the module in `scripts.swarm.tick` so it registers before the first
   pass, delete the hand written line, and add `tasks.raise_held(box)` after the
   group site if the site is new.

```python
from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import TASK_PASSES, held


def _example(tick: Any, get: Callable[[str], Any]) -> Any:
    return tick.skip_refused(tick.example.run, get("slug"), get("store"), get("doc"))


registry.register(CallStep("example", TASK_PASSES, 60, held(_example)))
```

## Tests

The feature map entry `swarm-tick-step-isolation` links the measured timing,
bounded group wait and spawn prerequisite behavior to the isolation tests.
The delivered proof and limitations are recorded in
[pull request 9278](https://github.com/the-cloud-clockwork/agentihooks/pull/9278).

| Test | What it holds |
| --- | --- |
| `tests/tick/test_step_isolation.py` | The 300 second predecessor scenario, completed results, abandoned wall time and daemon worker, budget calculation, undeclared timing refusal, registered timings, spawn prerequisites and longest path start bound |
| `tests/tick/test_tasks.py` | The task passes and close steps make the old calls in order, and a held exception skips the rest and is raised at the site |
| `tests/tick/test_place.py` | Group membership and orders; acks with and without sleep; preempt and spawn conditions for paused, events engine, panic and `tick_places`; the `spawn-due` delete; a held exception skipping every placement step |
| `tests/swarm/test_tick_groups.py` | The group sites run in the order of the table above |
| `tests/tick/test_observe.py` | The `spawn due` step places under the placing lock when the marker exists and sets it |
| `tests/tick/test_agents.py` | The agent steps in their groups, including the `close` steps before settle and close space |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Undeclared step timing` at import | Declare that exact step name in `MEASURED` before constructing its `CallStep` |
| A step is `late` while its call remains active | The waiting budget expired; the worker keeps running and its result is discarded. Check the live report and the call before changing the measured declaration |
| Spawn takes longer than forty seconds to start | Check legacy group order first; the forty second assertion grades the prerequisite path, not the sequential pass wall time |
| Changing `interval_s` does not reduce call frequency | The interval is declared metadata until cadence scheduling is applied |
| A step never runs | `engine.group_steps("<group>")` lists the registered steps of a group; a missing step means its module was not imported by `scripts.swarm.tick` |
| Import of the tick fails with `Duplicate step name` | Two registered steps share a name. Rename the new step; every `CallStep` writes only its own `actions:<name>` domain, so distinct names also keep `waves()` from refusing unordered writers |
| Spawning happens in the `observe` group, before the agents group | The previous pass left the `spawn-due` marker, meaning it did not reach the spawn step. Find what ended that pass early in the tick journal |
| Preempt and spawn run no calls | The swarm is paused, on the events engine or in panic, or, for spawn, `readiness.tick_places` refused; rank pass and bottleneck remedy still run |
| A tick pass raised and later steps of the same group did not run | Expected: the first moved step that raised holds the exception and the rest of the group's moved steps skip until it is raised at the site |
