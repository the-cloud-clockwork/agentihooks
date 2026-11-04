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
| Registry | `scripts.tick.registry` | Holds every registered step. `waves()` orders them by `order` and `needs` and refuses duplicate names, unknown dependencies, dependency cycles, a budget that is not a finite positive number, a negative interval, and two steps that write the same domain, or where one reads a domain the other writes, with no dependency between them, in one lane or across lanes |
| Group run | `scripts.tick.engine.run_group` | Runs the steps of one group sorted by `order`. Each step runs in a daemon thread under a copied context and a child deadline of its own budget; the engine waits at most that budget. A step that raises is recorded `failed`, one that runs past its budget is recorded `late`, and neither stops the next step |
| Tick site | `scripts.swarm.tick.run_group` | Runs one group with the pass state as keyword arguments, hands the engine the task appliers (`tasks.appliers(state)`) and returns the actions the steps wrote, then a line for each write the engine dropped, in step order. A group gets a 600 second deadline (`GROUP_BUDGET_S`) |
| Write applier | `run_group(..., apply=...)` | A map of domain to a function that applies one `Write`. As each step ends, the engine passes every write of a mapped domain to its applier; an applier that raises `RevisionConflict` drops that write. See [Steps that return writes](#steps-that-return-writes) |
| Call step | `scripts.tick.steps.CallStep` | A step that runs one call of the tick module. It reads the pass state through the view and writes the actions it returned to its own domain, `actions:<step name>` |
| Write step | `scripts.tick.steps.tasks.WriteStep` | A call step whose call returns action lines mixed with `Write` objects. It keeps every attribute of the `CallStep` it builds and splits the returned list: the strings go to `actions:<step name>`, the writes go to the engine |

Every `CallStep` runs in lane `tick` unless it is given `lane=SLOW`
(`scripts.tick.engine.SLOW`), with a declared budget and interval from
`scripts.tick.steps.budgets.timing_of(name)`. Dependencies default to empty;
spawn declares the prerequisites described below. Its constructor takes the step name, group, order and the
call, `call(tick_module, get)`, where `get(key)` reads one value of the pass
state. The call looks up the tick module's functions at run time, so tests
that patch a function on `scripts.swarm.tick` still reach it. An operation only
steps call, such as `_reap`, `_night_watch`, `_settle` or `_close_space`, lives
in the step module that owns its step (`agents`, `masters`, `tasks`), and a test
patches it there; the moved body still reaches tick seams through the imported
tick module.

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
| [`fleet red notice`](fleet-red-notice.md#the-notice-step-budget) | 19.981 s | 30 s | 60 s |

`CRITICAL_S`, `WIDE_S` and `SLOW_S` declare intervals of 60, 120 and 600
seconds. The group runner runs each group at its site on every pass and does
not skip a step because its interval has not elapsed.
`scripts.tick.engine.interval_s(step, cadence)` gives a step in lane `slow`
the larger of its declared interval and the slow lane cadence from
`AGENTIHOOKS_TICK_CADENCE`, and leaves every other step at its own interval;
no pass reads it yet. See
[Cadence](../pillars/swarm.md#cadence).

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
rolled back. Writes the step returns for the engine to apply, cache saves
included, are dropped with the result. A step that finishes past its budget
before the engine abandons it is recorded late and keeps its writes. A step
abandoned on every pass never saves its cache, so every pass repeats the work
the cache was meant to skip.
The [fleet red notice](fleet-red-notice.md#the-notice-step-budget) step
did this with its fifteen second unmeasured budget until it was measured.
This delivery allows the next pass to start the same step while
the earlier worker remains active. Do not interpret `late` as proof that the
underlying call stopped or that retrying it cannot overlap.

### The abandoned write journal line

Every step abandoned while still running prints one JSON line to standard
error when it declares write domains beyond its own `actions:<name>` domain.
The tick journal (`journalctl --user -t agentihooks-swarm`) keeps it:

```json
{"event": "swarm_tick_step_abandoned", "slug": "rig-grade-swarm", "step": "fleet red notice", "budget_s": 30.0, "writes": ["fleet red", "fleet red lifted", "fleet red listings"]}
```

| Field | Value |
| --- | --- |
| `event` | Always `swarm_tick_step_abandoned` |
| `slug` | The swarm of the pass, from `scripts.swarm.timing.SWARM`; `null` when the step ran outside a recorded tick |
| `step` | The step name |
| `budget_s` | The step's waiting budget in seconds, from `budgets.timing_of(<step>)[0]` |
| `writes` | Every write domain the step declares except `actions:<name>`, sorted. These are the writes the engine drops with the result |

`scripts.tick.engine._abandoned` writes the line at the two places a running
step is abandoned:

- `run_step` waited the full `budget_s` and the worker has not returned. This
  holds in a sequential pass and in a parallel pass alike.
- In a parallel pass the step's lane deadline ran out while the step was
  still running: 110 seconds for the critical lane, 400 for the slow lane.

A step dropped before it starts, because a step it needs was dropped or its
lane had already expired, writes no line: it never ran, so it had no writes to
lose. A `CallStep` declares only its `actions:<name>` domain unless it is given
more, so an abandoned call step with no extra writes also writes no line. A
`WriteStep` such as `rank pass`, `size pass`, `grouping release` or
`fleet red notice` always names the domains its writes would have changed.

Find the lines of the last twelve hours and count them per step:

```bash
journalctl --user -t agentihooks-swarm --since "-12h" \
  | grep '"event": "swarm_tick_step_abandoned"' \
  | sed 's/^[^{]*//' \
  | python3 -c 'import sys, json, collections
c = collections.Counter((r["slug"], r["step"], tuple(r["writes"])) for r in map(json.loads, sys.stdin))
for (slug, step, writes), n in c.most_common(): print(n, slug, step, ", ".join(writes))'
```

A step that appears on most passes loses its writes every time, as the fleet
red notice did before its budget was measured. Compare its wall time in
`agentihooks swarm <slug> tick-report --passes 10` with its budget and raise
its measured p95 in `budgets.MEASURED` through a pull request. Nothing reads
the line back; it is evidence for an operator or an agent, not an input to
the next pass.

### Lanes

A parallel pass gives each lane its own deadline: 110 seconds for the critical
lane, which holds every step whose lane is not `slow`, and 400 seconds for the
slow lane (`scripts.tick.pool.CRITICAL_S` and `SLOW_S`). These steps declare
the slow lane:

| Module | Group | Slow lane steps |
| --- | --- | --- |
| `scripts.tick.steps.report` | `wrapper events` | ledger event pass, done recheck, progress checks, waits end, merge pass, doc report, priorities sweep, health findings, findings pass |
| `scripts.tick.steps.report` | `wrapper metrics` | metrics record, bottleneck status |
| `scripts.tick.steps.tasks` | `reopen passes` | [fleet red notice](fleet-red-notice.md#the-slow-lane) |
| `scripts.tick.steps.place` | `placement notices` | lost runner, time left |

The other report steps (stall watch, overrun pass, quiet pass, inbox wake,
snapshot job, last tick, heartbeat, status publish) and every other step stay
critical. The command runner and the quota notice run before spawn and stay
critical.

Before spawn, the slow lane steps are the ones no placement step waits on, or
whose wait is bounded by their own budget. `lost runner` and `time left` have
no dependents, so the pass moves from `placement notices` straight to
`placement acks` while they run. `fleet red notice` is in `PREEMPT_NEEDS`, so
`preempt` and `spawn`, which needs `preempt`, wait for it: in a parallel pass
they are handed off behind it and start the moment it ends, while the rest of
`reopen passes`, all of `task passes`, the placement acks, the master seats,
`rank pass` and `bottleneck remedy` run without waiting. The notice step's
budget is 30 seconds, so `preempt` waits at most about 30 seconds after the
notice step started. When the notice step is abandoned at that budget,
`preempt` and `spawn` are dropped for the pass and recorded late, and the
next pass places; the `spawn-due` marker then makes the `spawn due` step place
first. `preempt` and `spawn` keep the critical deadline: when the critical
lane expires first they are dropped and recorded late too.

The move took these three steps off the critical chain. Its measured effect
on the pass is not recorded in this document; read it with
`agentihooks swarm <slug> tick-report --passes 10`.

The `health findings` step stores the findings it computes in the pass box
under `found`. The `status publish` step, in the `wrapper status` group, passes
`box.get("found")` to `cli._publish_tick_status`, and `status.status_report`
publishes those findings instead of calling `findings` again with the same
inputs. When the box holds no `found`, for example because the handed off
health step has not finished when the status publish runs, `status_report`
finds them itself as before. The status publish declares no need on the health
step, so it never waits for it. Over ten passes on 4 November 2026 after this
reached the workstation's dev checkout, the status publish median fell from
10.932 seconds to 9.244 seconds (p95 12.937 seconds).

In a parallel pass `run_group` takes the slow lane steps of a group, and every
step that needs one of them, out of the group. It runs the rest eagerly and
returns their records at once; the handed off steps start afterwards on a
daemon thread the pass owns (`Pass.detach`) and run eagerly on up to 8
workers. A handed off step that needs a step recorded late or dropped is
dropped and recorded late. Their records go to the `finished` callback; the
tick site passes one that prints their actions, then
`slow lane step <name> failed: <error>` for each failed step and
`a slow lane step held <repr>` when the pass box holds a held exception, printed
with its repr. When the
pass ends, `scripts.tick.pool.running` joins each handed off thread for at
most the slow lane time left plus `LATER_GRACE_S` (one second), one thread
after another, so each extra hung thread adds about one second. A step that
needs a step an earlier group handed off, and that is still running, is handed
off too and starts once that step ends; it is dropped when that step was
dropped or ran past its budget or lane. A need on a step of an earlier group
that ran critical and already ended is met at once. In sequential mode every step runs
in group order, each in its own daemon thread the caller waits on up to its
budget, slow lane included.

Lanes are threads, not processes. No lane process is spawned or killed at its
deadline; a hung slow step stays alive in its daemon thread after the pass has
joined it, and the next pass holds that step while it runs. Whether each lane
becomes a process is decided by the critical lane share in the tick report.

Give a new step the slow lane only when no critical step needs it:

```python
from scripts.tick.engine import SLOW

CallStep("example", "wrapper events", held(_example), lane=SLOW)
```

A `WriteStep` (`scripts.tick.steps.tasks.WriteStep`) takes the same `lane`
keyword and passes it to the `CallStep` it builds; without it the step runs in
lane `tick`:

```python
from scripts.tick.engine import SLOW
from scripts.tick.steps.tasks import WriteStep

WriteStep(
    "fleet red notice",
    REOPENS,
    held(_fleet_red_notice),
    writes=frozenset({FLEET_RED, FLEET_RED_LIFTED, fleet_red.LISTINGS}),
    needs=frozenset({"spawn due"}),
    reads=pass_domains("rows"),
    lane=SLOW,
)
```

A slow write step's writes are applied through the group's appliers on the
`tick-slow-lane` thread when it ends. If it is abandoned at its budget or its
lane deadline, its writes are dropped and the
[abandoned write journal line](#the-abandoned-write-journal-line) names them.

A critical step that needs a slow step is handed off with it, so it starts only
after that slow step ends, yet it keeps the critical deadline: if the critical
lane expires first it is dropped and recorded late. The registry checks domains across lanes, so a slow step
and a critical step that touch the same domain need a dependency between them:

```text
ValueError: Unordered writers in lanes tick and slow for tasks: other, example
ValueError: Unordered reader and writer in lanes tick and slow for tasks: other, example
```

The message names both lanes, in registration order, when the two steps sit in
different lanes, and one lane when they share it, as in
`Unordered writers in lane tick for tasks: other, example`.

`agentihooks swarm <slug> tick-report` prints each lane's share of step CPU
(see [Lane lines](tick-report.md#lane-lines)).

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

### Pass box edges

A value one step stores in the pass box and a later step reads is a declared
edge. Each pass box key has its own domain, `pass:<key>`, built with
`scripts.tick.steps.pass_domain("<key>")` or, for several keys,
`pass_domains("<key>", ...)`. Every step declares:

| Declaration | Rule |
| --- | --- |
| `writes` | Each `pass:<key>` the step stores |
| `reads` | Each `pass:<key>` the step reads |
| `needs` | The step that writes each key it reads, or a step that already needs that writer |

The registry checks these with the other domains when it builds `waves()`.
A reader and a writer of the same `pass:<key>` with no need path between them
raise `Unordered reader and writer in lane <lane> for pass:<key>: <first>, <second>`
(`lanes <a> and <b>` when the two steps sit in different lanes),
and two steps that store the same key with no need path raise
`Unordered writers in ...`. A pass box hand off between steps that could run in
either order is therefore refused when the step pool is first resolved
(`registry.steps()`, `waves()` or `validate()`, which `engine.group_steps`
calls), before any step of that pass runs.

The delivered writers of each key:

| Key | Written by | Example readers |
| --- | --- | --- |
| `config` | `config load`, then `stopped branch` and `drained resume` | `master wake`, `master stall`, `strays`, `orphans`, placement notices |
| `finished` | `stopped branch` | The `start` group site in `scripts.swarm.tick`, which ends the pass |
| `doc`, `rows` | `ledger state`; `spawn due` and `spawn` rewrite `rows` | `automated cleanup`, `launch checks`, `sleeping`, `preempt`, `spawn` |
| `sleeping` | `sleeping` | `drained resume`, `dispatch seat`, `night watch`, `codex hook order` |
| `agents` | `page relay` | `stall watch`, `inbox wake` |
| `view` | `tick view` | The slow lane event steps, `health findings`, `metrics record`, `status publish` |
| `live` | `stall watch` | `overrun pass` |
| `found` | `health findings` | `findings pass`, `metrics record`, `status publish` |
| `judged`, `stop`, `started` | `intent check`, `ledger health`, `snapshot job` | No step; `stop` is read by `scripts.swarm.cli.run_tick` |

Because the readers of `config` need `drained resume`, that step carries
`DRAINED_RESUME_NEEDS` (`scripts.tick.steps.tasks`) plus `sleeping`: every step
that reads `config` before it, so the rewritten config is ordered after them.
`preempt` needs `PREEMPT_NEEDS` in `scripts.tick.steps.place`, which includes
`drained resume`, so it reads the rewritten config.

A new step that reads a pass box value declares it like this:

```python
from scripts.tick import registry
from scripts.tick.steps import CallStep, pass_domains
from scripts.tick.steps.tasks import TASK_PASSES, held

registry.register(
    CallStep(
        "example",
        TASK_PASSES,
        held(_example),
        frozenset({"ledger state"}),
        pass_domains("doc"),
        pass_domains("example"),
    )
)
```

The positional order is name, group, call, `needs`, `reads`, `writes`.
`WriteStep` takes the same `needs` and `reads` as keywords.

`tests/tick/test_pass_edges.py` replays the twelve tick scenarios through
`scripts.swarm.tick.tick` and `scripts.swarm.cli.run_tick` with a recording
pass box and recording pass views. It records which step stored and read each
key and names every gap:

- `<step> reads pass <key> without declaring it`
- `<step> reads pass <key> without needing its writer [<writers>]`
- `<step> writes pass <key> without declaring it`

It also fails when a registered step never ran in any scenario. The feature map
entry `swarm-tick-pass-edges` links this behavior to the test; the delivered
change is
[pull request 9623](https://github.com/the-cloud-clockwork/agentihooks/pull/9623).

## Groups and their sites

The groups run in this order in one pass. Groups that hold the task, planning
and placement steps are in bold; the others hold steps from other changes.

| Order | Group | Site in the pass | Steps |
| --- | --- | --- | --- |
| 1 | `start` | First line of the pass | config load, stopped branch |
| 2 | `pass setup` | After `start`, unless the stopped branch ended the pass | spawn hold clear, pass budget |
| 3 | `observe` | After `pass setup` | pane evidence through verify, including **spawn due** at order 70 |
| 4 | `agents` | After the observation sweeps | reap, recover outages, strays, failed panes |
| 5 | `masters` | After the agents | master wake, retire idle master, master stall, prompt stall |
| 6 | `drained resume` | After the masters | sleeping, drained resume |
| 7 | `tasks` | After `drained resume` | orphans |
| 8 | `reopen passes` | After `tasks` | dev red reopen, prerequisite reopen, promotion refresh, release alert, [fleet red notice](fleet-red-notice.md) (slow lane) |
| 9 | **`task passes`** | After `reopen passes` | run window, verification notices, size pass, grouping release, dispatcher group |
| 10 | `place` | First line inside the placing lock | |
| 11 | `placement notices` | After `place`, inside the placing lock | quota notice, lost runner (slow lane), time left (slow lane) |
| 12 | **`placement acks`** | After `placement notices`, inside the placing lock | collect acks, codex hook order |
| 13 | `master seats` | Inside the placing lock, only when the swarm is not sleeping | master down, tick master, master scale, seat alarm |
| 14 | **`placement`** | After the master seats, inside the placing lock, only when the swarm is not sleeping | rank pass, bottleneck remedy, preempt, spawn |
| 15 | `dispatch seat` | Last site inside the placing lock | dispatch seat |
| 16 | `master watch` | After the placing lock | night watch, master alarm |
| 17 | `close` | Last line of the pass | conversations, session models, handoff transfers, **settle**, **close space** |

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
| 30 | size pass | `tasks.size_writes` returns a `sizes` task write per task `difficulty.sizes` sizes, applied by `tasks.task_writer` only while the task is still unsized |
| 40 | grouping release | `tasks.release_writes` returns a `groups` write per lead `grouping.releases` finds stopped, applied by `tasks.group_writer` only while the lead still holds the members it read |
| 50 | dispatcher group | `dispatcher.group` |

### Placement (`scripts.tick.steps.place`)

| Group | Order | Step | Call and condition |
| --- | --- | --- | --- |
| `placement acks` | 10 | collect acks | `_collect_acks`, every pass |
| `placement acks` | 20 | codex hook order | `_codex_hook_order`, skipped while the swarm sleeps |
| `placement` | 10 | rank pass | `tasks.rank_writes` returns `dispatcher.rank_decision` as one `rank` write; the rank applier takes it with `dispatcher.apply_ranking` only while the saved claim queue heads it read still hold, else drops it for the next pass |
| `placement` | 20 | bottleneck remedy | `bottleneck_remedy.step` |
| `placement` | 30 | preempt | `preempt.run` with the seats view, only when the swarm is `running`, not on the events engine and no panic is set |
| `placement` | 40 | spawn | `_spawn` under the placement pass, only when the swarm is `running`, `readiness.tick_places` allows the engine to place and no panic is set. After it spawns, it deletes the `spawn-due` marker |

The `spawn-due` marker pairs the spawn step with the `spawn due` step of the
`observe` group (`step_spawn_due` in `scripts.tick.steps.observe`).
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

## Steps that return writes

Under the parallel engine two steps of one group can run at the same time, so a
step that writes the ledger itself can overwrite what a concurrent step wrote
from the same read. A write step instead computes its change from the pass
state and returns it. The engine applies it with compare and set: the write
lands only while what the step read still holds the value it saw.

### The write

`scripts.tick.step.Write` carries four fields:

| Field | Meaning |
| --- | --- |
| `domain` | What the write changes; selects the applier |
| `key` | The record: a task id, a phase id, the swarm or the master name |
| `value` | The change to make |
| `expected` | The guard: what the step read. Defaults to `None`, which the task appliers read as no fields to compare |

These steps return writes. Every domain constant except `fleet_red.LISTINGS`
lives in `scripts.tick.steps.tasks`.

| Step (group) | Domain | Key | Guard (`expected`) | Applier and what it does on a match |
| --- | --- | --- | --- | --- |
| size pass (`task passes`) | `sizes` | task id | `{"difficulty": ""}`: the task is still unsized | `task_writer`: `ledger.update_task` with the guard as `if_match` |
| grouping release (`task passes`) | `groups` | lead task id | `{"group_members": [...]}`: the lead still holds the members the step read | `group_writer`: `ledger.ungroup_tasks` with the guard as `if_match` |
| fleet red notice (`reopen passes`, slow lane) | `fleet red` | task id | `{"state": "open", "pr_url": url, "claimed_by": ""}` | `fleet_red_writer`: posts the hold notice only while no seat claimed the task |
| fleet red notice | `fleet red lifted` | swarm | none | `lifted_writer`: deletes the hold marks the write names, one per line |
| fleet red notice | `fleet red listings` | listing key | none | `fleet_red.saver`: saves the open pull request listing |
| rank pass (`placement`) | `rank` | swarm | the saved claim queue heads the pass read | `rank_writer`: `dispatcher.apply_ranking`; a pass that lands no heads only proposes ranks and takes no guard |
| planning and phase pass (`wrapper phases`) | `phases` | phase id | `not change.done`: the done state the step read | `phase_writer`: marks the pass box and sets the checkbox through `phases.apply_change` |
| master stall (`masters`) | `master stall` | master name | the master name the step read | `stall_writer`: `master_stall.apply_stall` while that master is still the live master |

Each step reads the pass state and leaves it as read. `difficulty.sizes(doc)`
yields each unsized open task's fields (at most `PER_TICK`, five, per pass);
`grouping.releases(slug, store, doc)` returns each stopped lead with the reason
and its members; `fleet_red.holds` returns new holds, lifted marks and listing
saves; `dispatcher.rank_decision` returns one `Ranking`;
`phases.phase_changes` returns `PhaseChange` items; `master_stall.stalls`
returns `Stall` items.

### How the engine applies writes

1. The step ends. In a parallel pass the engine applies its writes on the
   scheduling thread before any step that needs it starts; outside one, after
   the step and before the next. A slow lane step's writes are applied through
   the group's appliers on the slow lane thread.
2. For each write whose domain has an applier, the applier compares its guard.
   The task, group, fleet red and phase appliers read the record from the
   pass's held ledger view (`scripts.tick.view.current()`) as it stands after
   the pass's latest write. The rank applier reads the saved heads from Redis;
   the stall applier reads the live master name.
3. A match makes the call. The size and group appliers also send the guard to
   the ledger server as `if_match`, which compares the same fields in the same
   apply: the server refuses an ungroup whose members moved, and leaves a task
   update unapplied when a guarded field moved. The size applier then checks
   the task the server returned and treats a guarded field that does not hold the
   new value as moved.
4. A guarded value that moved, a refused ledger call (`LedgerRefused`), a
   failed fleet red notice, or a write whose value is not the expected type
   raises `RevisionConflict`. A failed fleet red notice first deletes its hold
   mark, so the next pass posts it again.
5. The engine catches `RevisionConflict`, adds the write to the step record's
   `dropped` and the reason to `conflicts`, and goes on with the next write.
   Any other exception from an applier is not caught. Action lines an applier
   returns (rank, phase, stall) are kept on the record as `applied`.

`tasks.appliers(state)` builds the map for a group site. With no `ledger` or no
`slug` in the pass state it returns nothing. With both it maps `sizes` and
`groups`; with a `store` also `fleet red`, `fleet red lifted`,
`fleet red listings` and `rank`; with `now_ms` as well, `master stall`; with an
`inbox` and a `box` as well, `phases`. `swarm.tick.run_group`, the masters
group run and the `wrapper` group runs of the swarm command all pass this map.

### Dropped writes and the recompute

A dropped write is not retried in the same pass. `steps.actions(records)`
returns each step's own actions, then the action lines its appliers returned,
then one line per dropped write:

```text
dropped write sizes t7 of step size pass: task t7 changed difficulty since the step read it; the next pass recomputes it
dropped write groups t3 of step grouping release: task t3 changed group_members since the step read it; the next pass recomputes it
dropped write sizes t7 of step size pass: the ledger refused the write of task t7; the next pass recomputes it
dropped write rank s1 of step rank pass: the claim queue heads of swarm s1 changed since the rank pass read them; the next pass recomputes it
dropped write phases p2 of step planning and phase pass: phase p2 changed its checkbox since the phase pass read it; the next pass recomputes it
dropped write master stall master@s1-2 of step master stall: master master@s1-2 is no longer the live master of swarm s1; the next pass recomputes it
```

The line joins the pass actions in the tick journal. Each step record also
lands in the `tick:records` stream with its `dropped` and `conflicts` counts,
and `swarm <id> tick-report` sums them in its Dropped column (see
[Outcome columns](tick-report.md#outcome-columns)). The step's own action
line, such as `sized task t7 M by rule`, is still reported for that pass even
though its write was dropped, so read both lines together.

The next pass reads the ledger again and runs the step on the new state. A task
another writer sized is no longer unsized, so the size pass skips it; a task
still unsized gets a fresh size. A lead whose members changed is judged on its
new members. A rank pass, phase checkbox, fleet red hold or stall decision is
decided again from the new state. No queue of dropped writes is kept.

### Conflict scenario

`tests/tick/test_apply.py` holds the scenario both sequentially and on a
parallel pass:

1. Task `t1` is at revision 1. The step reads it and returns a write expecting
   revision 1; a concurrent writer moves `t1` to revision 2 before the write is
   applied.
2. The applier raises `RevisionConflict`. The record's `dropped` holds the write,
   the store keeps the concurrent value, and the actions read
   `sized from revision 1` followed by
   `dropped write tasks t1 of step size: tasks/t1 moved from revision 1 to 2; the next pass recomputes it`.
3. The next pass reads revision 2, returns a write expecting it, and the write
   lands: `t1` is at revision 3 with `M from revision 2` and nothing is dropped.

`tests/tick/test_apply_rank.py`, `tests/tick/test_apply_phases.py`,
`tests/tick/test_apply_fleet_red.py` and `tests/tick/test_apply_stall.py` run
the same drop and recompute for a rank pass whose heads another pass moved, a
phase checkbox another writer moved, a hold a seat claimed mid pass and a
stall of a master another master replaced.

### Writing a step that returns writes

Return action strings and `Write` objects from the call and register it as a
`WriteStep` that declares its write domain, so `waves()` can order it against
other writers of that domain:

```python
from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Write
from scripts.tick.steps.tasks import SIZES, TASK_PASSES, WriteStep, held


def _example(tick: Any, get: Callable[[str], Any]) -> list[str | Write]:
    return [
        Write(SIZES, "t7", {"difficulty": "S", "difficulty_source": "rule"}, expected={"difficulty": ""}),
        "sized task t7 S by rule",
    ]


registry.register(WriteStep("example", TASK_PASSES, held(_example), writes=frozenset({SIZES})))
```

A write to a domain with no applier at the site is neither applied nor dropped:
it stays on the record's result and changes nothing. A new domain needs its
applier added to `tasks.appliers`. The step name still needs its timing entry,
as described above.

### Limits

- The held view compare sees only changes up to the view's latest refresh.
  Without a held view the task, group, fleet red and phase appliers skip that
  compare; a task the view lacks compares as empty. The size and group writes
  are still compared on the server through `if_match`; the fleet red, phase,
  rank and stall writes are not.
- A dropped write waits a whole pass for its recompute.
- `LedgerRefused` in a write step's applier is a dropped write with a
  `dropped write` line, where the old in-step calls produced a
  `skipped ...: the ledger refused its write` line through `skip_refused`.

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
| `tests/tick/test_tasks.py` | The task passes and close steps make the old calls in order, a held exception skips the rest and is raised at the site, `fleet red notice` sits in lane `slow` with every other task step in lane `tick`, and a `WriteStep` keeps the lane it is given |
| `tests/tick/test_place.py` | Group membership and orders; `lost runner` and `time left` in lane `slow` and every other placement step in lane `tick`; acks with and without sleep; preempt and spawn conditions for paused, events engine, panic and `tick_places`; the `spawn-due` delete; a held exception skipping every placement step |
| `tests/tick/test_abandoned_writes.py` | A write step abandoned at its budget, and one abandoned when its lane expired, each journal one `swarm_tick_step_abandoned` line with the swarm, step, budget and sorted write domains; a call step abandoned with only its actions domain journals nothing |
| `tests/tick/test_apply.py` | The dropped write and its recompute, sequential and parallel; writes without an applier pass through; both appliers write while their guard holds, drop a moved guard and turn a refused ledger call into a conflict; appliers need a ledger and a swarm; the size pass and grouping release return guarded writes and leave the document as read; a write step keeps every attribute of its call step |
| `tests/tick/test_apply_rank.py` | The rank pass returns one write guarded on the heads it read; the applier takes it while the saved heads hold, takes a proposing pass whatever the heads, drops moved heads, and the engine keeps applied actions after the step actions and beside a later dropped write |
| `tests/tick/test_apply_phases.py` | A write per checkbox guarded on the done state read; the applier marks the box and sets the checkbox, writes through without a held view, and drops a moved, lost or refused checkbox |
| `tests/tick/test_apply_fleet_red.py` | Guarded hold writes, lifted and listing writes; the applier posts while the task stays open and unclaimed, drops a moved or claimed task, and forgets a failed notice so the next pass posts it again |
| `tests/tick/test_apply_stall.py` | The stall pass decides and writes nothing; the applier drops a stall once another master holds the seat, recomputed on the next pass |
| `tests/swarm/test_tick_groups.py` | The group sites run in the order of the table above |
| `tests/tick/test_observe.py` | The `spawn due` step places under the placing lock when the marker exists and sets it |
| `tests/tick/test_agents.py` | The agent steps in their groups, including the `close` steps before settle and close space, and the tick site printing the actions and failures of handed off slow steps |
| `tests/tick/test_pass_edges.py` | Every pass box read over the twelve tick scenarios is declared with a need on its writer and every pass box write is declared; a planted undeclared read and a planted missing need are named |
| `tests/tick/test_registry.py` | Unordered writers and crossed readers refused within a lane and across lanes, with the lane names in the message |
| `tests/tick/test_report.py` | The slow lane report steps and a `CallStep` keeping the lane it is given |
| `tests/tick/test_pool.py` | A parallel group returning its critical records while its slow step runs, a handed off step starting after the critical step it needs and dropped when that step was dropped, a handed off step applying its writes through the group appliers on a daemon thread named `tick-slow-lane`, the pass waiting for a hung slow step only until the slow deadline plus the grace second, and a sequential pass running slow steps in the group |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Undeclared step timing` at import | Declare that exact step name in `MEASURED` before constructing its `CallStep` |
| A step is `late` while its call remains active | The waiting budget expired; the worker keeps running and its result is discarded. Check the live report and the call before changing the measured declaration |
| Spawn takes longer than forty seconds to start | Check legacy group order first; the forty second assertion grades the prerequisite path, not the sequential pass wall time |
| Changing `interval_s` or `AGENTIHOOKS_TICK_CADENCE` does not reduce call frequency | Expected: every pass runs every registered step; `interval_s` and the cadence are validated but no pass skips a step on them yet |
| `Unordered writers in lanes tick and slow` on the first group run | A slow step and a critical step touch the same domain with no dependency; add the step that must run first to `needs`; moving both into one lane does not clear it |
| `Unordered reader and writer ... for pass:<key>` on the first pass | A step reads a pass box key with no need path to the step that writes it; add that writer, or a step that needs it, to the reader's `needs` |
| `test_pass_edges` names `reads pass <key> without declaring it` | The step reads that key from the box or its pass view; add `pass_domain("<key>")` to its `reads` and its writer to its `needs` |
| A critical step is recorded late with no error | It needs a slow lane step, so it was handed off and waited for that step while the critical deadline ran out. Drop the need or keep the needed step critical |
| `swarm_tick_step_abandoned` in the tick journal | That step was still running at its budget or its lane deadline and the writes it names were dropped. Its worker may still finish its external calls. One line now and then is a slow call; the same step on most passes means its budget is below its real wall time, see [The abandoned write journal line](#the-abandoned-write-journal-line) |
| A step is late in `tick-report` but no `swarm_tick_step_abandoned` line exists | The step declares no write beyond its own `actions:<name>` domain, or it was dropped before it started because a step it needs was dropped or its lane had expired. Neither loses writes |
| Preempt and spawn are late in a pass with no critical lane overrun | `fleet red notice` ran past its 30 second budget; both wait for it and are dropped when it is abandoned. Look for its `swarm_tick_step_abandoned` line in the same pass |
| `slow lane step <name> failed` in the tick journal | That handed off step raised; its error follows the colon. The critical work of the pass already finished |
| A step never runs | `engine.group_steps("<group>")` lists the registered steps of a group; a missing step means its module was not imported by `scripts.swarm.tick` |
| Import of the tick fails with `Duplicate step name` | Two registered steps share a name. Rename the new step; every `CallStep` writes only its own `actions:<name>` domain, so distinct names also keep `waves()` from refusing unordered writers |
| Spawning happens in the `observe` group, before the agents group | The previous pass left the `spawn-due` marker, meaning it did not reach the spawn step. Find what ended that pass early in the tick journal |
| Preempt and spawn run no calls | The swarm is paused, on the events engine or in panic, or, for spawn, `readiness.tick_places` refused; rank pass and bottleneck remedy still run |
| A `dropped write` line in the tick actions | Expected under a concurrent change: the guarded field moved or the ledger refused the call. The next pass recomputes it; a line that repeats every pass for the same task means something keeps changing that field, or the ledger keeps refusing the write |
| A size or group change never lands and no `dropped write` line appears | The site had no `ledger` or `slug` in its pass state, so `tasks.appliers` returned no appliers and the writes stayed on the record |
| A tick pass raised and later steps of the same group did not run | Expected: the first moved step that raised holds the exception and the rest of the group's moved steps skip until it is raised at the site |
