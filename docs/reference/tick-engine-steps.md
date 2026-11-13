---
title: Tick engine steps
parent: Reference
nav_order: 67
---

# Tick engine steps

The swarm tick (`scripts.swarm.tick.tick`) runs its work as registered engine
steps. Every step follows one pattern: a name, a group, a lane, a budget row, an
interval, the steps it needs, the domains it reads and writes, the ports its
effects name and a feature map id, with a feature test and a scenario index row.
`python -m scripts.tick.new_step` writes all of it at once (see
[Adding a step](#adding-a-step)) and the [tick pattern gate](tick-pattern-gate.md)
refuses a step that leaves the pattern. The
[tick step catalogue](tick-step-catalogue.md) lists every registered step with
those fields, rendered from the registry. This page covers the engine, the step
types, budgets and intervals, the lanes including closing, the pass state and
records, and the task, planning and placement steps that moved out of the hand
written pass.

## The engine

| Part | Module | What it does |
| --- | --- | --- |
| Registry | `scripts.tick.registry` | Holds every registered step. `waves()` orders them in dependency waves by `needs` and refuses duplicate names, unknown dependencies, dependency cycles, a budget that is not a finite positive number, a negative interval, and two steps that write the same domain, or where one reads a domain the other writes, with no dependency between them, in one lane or across lanes |
| Group run | `scripts.tick.engine.run_group` | Runs the steps of one group in dependency order: a sequential pass one at a time, a parallel pass each step once its needs have ended. Each step runs in a daemon thread under a copied context and a child deadline of its own budget; the engine waits at most that budget. A step that raises is recorded `failed`, one that runs past its budget is recorded `late`, and neither stops the next step |
| Graph run | `scripts.tick.engine.run_graph` | Runs a whole step list, across groups, in the schedule `run_group` uses for one group. Steps are ordered by `registry.ordered`: each after the steps among them it needs, otherwise in the order given. An optional `gate` can shut a step as it becomes ready. See [The flat pass graph](#the-flat-pass-graph) |
| Site edges | `scripts.tick.registry.site_edges` | Returns the steps with each step of a group site also needing every step of the nearest earlier site that holds steps, so a graph run keeps the site order |
| Site gate | `scripts.tick.engine.SiteGate` | The `gate` of a graph run: runs a site's hook once, when the site's first step is ready, and shuts single steps through its `shut` check |
| Pass sites | `scripts.tick.sites.run_sites` | Runs every registered step of a site table as one graph with site edges, a `SiteGate` and the appliers it is given. `run_tick` and `tick` both run their pass through it |
| Tick site | `scripts.swarm.tick.run_group` | Runs one group with the pass state as keyword arguments, hands the engine the task appliers (`tasks.appliers(state)`) and returns, per step in order, its own actions, then the action lines its appliers returned, then a line for each of its writes the engine dropped. A group gets a 600 second deadline (`GROUP_BUDGET_S`) |
| Write applier | `run_group(..., apply=...)` | A map of domain to a function that applies one `Write`. As each step ends, the engine passes every write of a mapped domain to its applier; an applier that raises `RevisionConflict` drops that write. See [Steps that return writes](#steps-that-return-writes) |
| Call step | `scripts.tick.steps.CallStep` | A step that runs one call of the tick module. It reads the pass state through the view and writes the actions it returned to its own domain, `actions:<step name>` |
| Write step | `scripts.tick.steps.tasks.WriteStep` | A call step whose call returns action lines mixed with `Write` objects. It keeps every attribute of the `CallStep` it builds and splits the returned list: the strings go to `actions:<step name>`, the writes go to the engine |

Every `CallStep` runs in lane `tick` unless it is given `lane=SLOW`
(`scripts.tick.engine.SLOW`), with a declared budget and interval from
`scripts.tick.steps.budgets.timing_of(name)`. Dependencies default to empty;
spawn declares the prerequisites described below. Its constructor takes the step name, group and the
call, `call(tick_module, get)`, where `get(key)` reads one value of the pass
state. The call looks up the tick module's functions at run time, so tests
that patch a function on `scripts.swarm.tick` still reach it. An operation only
steps call, such as `_reap`, `_night_watch`, `_settle` or `_close_space`, lives
in the step module that owns its step (`agents`, `masters`, `tasks`), and a test
patches it there; the moved body still reaches tick seams through the imported
tick module.

## Live budgets, floors and declared intervals

The table `scripts.tick.steps.budgets.FLOORS` declares each step's floor and its
interval. Each pass, `run_tick` calls `budgets.resolve(store, slug)` before the
first group. It reads every step's window of its last sixty walls, the Redis list
`tick:walls:<step>`, and stores the live budgets for the pass. `budget_of(name)`,
read through `CallStep.budget_s`, returns `max(floor, ceil(p95 * 1.5))` once a
step has sixty walls, and the floor until then. `scripts.swarm.timing.append_step`
pushes each budgeted step's wall to its window in the same pipeline as its
`tick:steps` record and trims the window to sixty. When a window is missing,
`resolve` rebuilds it from the `tick:steps` stream, at most once every ten
minutes. The stream stays the record.

| Step example | Floor | Declared interval |
| --- | --- | --- |
| `reap` | 22 s | 60 s |
| `spawn` | 33 s | 60 s |
| `night watch` | 22 s | 120 s |
| `metrics record` | 29 s | 600 s |
| [`fleet red notice`](fleet-red-notice.md#the-notice-step-budget) | 30 s | 60 s |

The six heavy rows keep their earlier measured budgets as floors. Every row that
measured under one second, and every row that was never measured, has a two
second floor. `scripts.tick.registry.latest_start` adds floors, so the spawn
start bound does not move with live budgets.

`CRITICAL_S`, `WIDE_S` and `SLOW_S` declare intervals of 60, 120 and 600
seconds. `scripts.tick.engine.interval_s(step, cadence)` gives a step in lane
`slow` the larger of its declared interval and the slow lane cadence from
`AGENTIHOOKS_TICK_CADENCE`, and leaves every other step at its own interval.
The group runner skips a step whose interval has not elapsed; see
[Interval enforcement](#interval-enforcement) and
[Cadence](../pillars/swarm.md#cadence).

Every step needs an explicit `FLOORS` entry. An unknown name raises
`ValueError: Undeclared step timing: <name>` when `CallStep` is constructed,
before registration.

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

Outside a pass the result is the floor, `(22.0, 60.0)`: budget first,
interval second. To change a floor or interval, update the step's
`Timing(floor_s, interval_s)` entry in `FLOORS` through the normal code review
and CI workflow. Declare a newly registered `example` step before constructing it:

```python
"example": Timing(2.0, CRITICAL_S),
```

Its live budget then follows its walls once sixty runs are recorded.

### Interval enforcement

At the start of each group, `scripts.tick.engine.resting(ordered, cadence)`
names the steps of that group that rest this pass. A step can rest only when
its interval from `interval_s(step, cadence)` is above one cron minute
(`CRON_S`, 60 seconds). It rests while the pass start is no more than its
interval less one cron minute after the end of its last ended run. Outside a
recording pass the clock stands in for the pass start.

| Interval | Rests while the last end is at most | Runs |
| --- | --- | --- |
| 60 s (`CRITICAL_S`) | never rests | every pass |
| 120 s (`WIDE_S`) | 60 s before the pass start | every other pass |
| 600 s (`SLOW_S`) | 540 s before the pass start | about one pass in ten |
| Slow lane step declared at 60 s, `AGENTIHOOKS_TICK_CADENCE=3` | 120 s before the pass start | one pass in three |

Subtracting one cron minute means a step whose run ended a few seconds into a
pass is due again on the pass that starts one interval after the earlier one,
rather than one pass later.

A resting step starts no thread and calls nothing. It ends at once, so steps
that `need` it start as if it had run. Its `tick:records` row has outcome
`skipped`, its `skipped` and `rested` fields true, `abandoned` false, a wall time of zero
and its budget; see
[Tick report](tick-report.md#outcome-columns). This holds in the sequential
runner, in the parallel eager runner and for slow lane steps handed off to run
after the critical lane.

The end of a run is stored only while the pass is recording, and only for a
step with an interval above one cron minute or a step in lane `slow`. It is
written when the step's record is journalled, as epoch seconds, into the hash
`tick:step-ran` under the swarm's Redis key prefix, one field per step name
(`scripts.swarm.timing.step_ended` and `step_ends`).

| Outcome of the run | Records an end | Next pass |
| --- | --- | --- |
| `ok` | yes | rests inside the interval |
| `failed` | yes | rests inside the interval |
| `late` (abandoned at its budget or lane deadline, or finished past its budget) | no | due, since the stored end is the one that let it run |
| `held` (earlier run still alive) | no | due, since the stored end is the one that let it run |
| Dropped before it started, or rested | no | keeps the earlier end |

A step with no stored end is due. Nothing rests when the pass has no
recording store or swarm, and a Redis failure reading the hash makes every
step due. A failed write prints
`{"event": "swarm_tick_record_failed", "slug": "<swarm>", "stream": "tick:step-ran"}`
on stderr and leaves the earlier end in place. A group of steps all at one
cron minute does not read the hash.

A step that writes the pass state must run every pass, because the steps
after it read that state from the same pass. The registry refuses a step
whose interval is above 60 seconds (`registry.PASS_BOX_INTERVAL_S`) and that
writes a `pass:` domain:

```text
ValueError: Interval of <step> above 60s writes pass:<key>
```

The fix is to declare that step at `CRITICAL_S` in `FLOORS`. `ledger health`,
which writes `pass:stop`, is declared at `CRITICAL_S` for that reason, and
`snapshot job` moved to `CRITICAL_S` in the same delivery.

Read the stored ends of a swarm from the repository root:

```bash
~/dev/tcc-ecosystem/.venv/bin/python -c 'from scripts.swarm import store, timing; print(timing.step_ends(store.connect(), "rig-grade-swarm"))'
```

The result maps each step name to the epoch seconds its last ended run
finished, for example `{"pane evidence": 1793870400.12, "metrics record": 1793870160.5}`.
Deleting a field makes that step due on the next pass. Count how often a step
rested in the latest passes with the report:

```bash
agentihooks swarm rig-grade-swarm tick-report --passes 60
```

A 120 second step shows about half its records in the `Skipped` column, a 600
second step about nine in ten, and a critical step none.

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
The tick journal (`TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-1h" --until now`) keeps it:

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
  still running: 60 seconds for the critical lane, 59 for the slow lane.

A step dropped before it starts, because a step it needs was dropped or its
lane had already expired, writes no line: it never ran, so it had no writes to
lose. A `CallStep` declares only its `actions:<name>` domain unless it is given
more, so an abandoned call step with no extra writes also writes no line. A
`WriteStep` such as `rank pass`, `size pass`, `grouping release` or
`fleet red notice` always names the domains its writes would have changed.

Find the lines of the last twelve hours and count them per step:

```bash
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-12h" --until now \
  | grep '"event": "swarm_tick_step_abandoned"' \
  | sed 's/^[^{]*//' \
  | python3 -c 'import sys, json, collections
c = collections.Counter((r["slug"], r["step"], tuple(r["writes"])) for r in map(json.loads, sys.stdin))
for (slug, step, writes), n in c.most_common(): print(n, slug, step, ", ".join(writes))'
```

A step that appears on most passes loses its writes every time, as the fleet
red notice did before its budget was measured. Compare its wall time in
`agentihooks swarm <slug> tick-report --passes 10` with its budget and raise
its floor in `budgets.FLOORS` through a pull request. Nothing reads
the line back; it is evidence for an operator or an agent, not an input to
the next pass.

The tick report counts these runs per step in its Late share, Abandoned, Held and Dropped columns; see
[Outcome columns](tick-report.md#outcome-columns).

### Lanes

A parallel pass gives each lane its own deadline: 60 seconds for the critical
lane, which holds every step whose lane is neither `slow` nor `closing`, and
59 seconds for the slow lane (`scripts.tick.pool.CRITICAL_S` and `SLOW_S`),
both counted from the start of the pass. The closing lane's 30 seconds count
from the start of the pass `finally`; see [The closing lane](#the-closing-lane).
These steps declare the slow lane:

| Module | Group | Slow lane steps |
| --- | --- | --- |
| `scripts.tick.steps.report` | `wrapper events` | ledger event pass, done recheck, progress checks, waits end, merge pass, doc report, priorities sweep, health findings, findings pass |
| `scripts.tick.steps.report` | `wrapper metrics` | metrics record, bottleneck status |
| `scripts.tick.steps.tasks` | `reopen passes` | [fleet red notice](fleet-red-notice.md#the-slow-lane) |
| `scripts.tick.steps.place` | `placement notices` | lost runner, time left |

Last tick, heartbeat and status publish declare the closing lane. The other
report steps (stall watch, overrun pass, quiet pass, inbox wake, snapshot job)
and every other step stay critical. The command runner and the quota notice run
before spawn and stay critical. The quota notice step reads cached quota and
runs no Claude or Codex quota probe; the quota prober service writes those
caches once a minute ([Quota prober](quota-prober.md)).

Before spawn, the slow lane steps are the ones no placement step waits on, or
whose wait is bounded by their own budget. `lost runner` and `time left` have
no dependents, so the pass moves from `placement notices` straight to
`placement acks` while they run. `fleet red notice` needs `spawn`, the
reverse of the old order: in a parallel pass it is handed off behind `spawn`
and starts the moment `spawn` ends, while the rest of `reopen passes`, all of
`task passes`, the placement acks, the master seats, `rank pass` and
`bottleneck remedy` run without waiting on it. No step needs `fleet red
notice` in turn, so nothing waits on it and nothing is dropped with it. When
the notice step is abandoned at its 30 second budget, only its own writes are
dropped and recorded late for that pass.

The move took these three steps off the critical chain. Its measured effect
on the pass is not recorded in this document; read it with
`agentihooks swarm <slug> tick-report --passes 10`.

The `health findings` step stores the findings it computes in the pass box
under `found`. The `status publish` step, in the `closing` group, passes
`box.get("found")` to `cli._publish_tick_status`, and `status.status_report`
publishes those findings instead of calling `findings` again with the same
inputs. When the box holds no `found`, for example because the handed off
health step has not finished when the status publish runs, `status_report`
finds them itself as before. The status publish needs the health step and is
kept, so it waits for it at most its own budget and runs even when the health
step was dropped (see [Needs across groups](#needs-across-groups)). Over ten passes on 4 November 2026 after this
reached the workstation's dev checkout, the status publish median fell from
10.932 seconds to 9.244 seconds (p95 12.937 seconds).

In a parallel pass `run_group` takes the slow lane steps of a group, and every
step that needs one of them, out of the group. It runs the rest eagerly and
returns their records at once; the handed off steps start afterwards on a
daemon thread the pass owns (`Pass.detach`) and run eagerly on up to
`workers` pool threads, 8 by default (see [Pass executors](#pass-executors)).
A handed off step that needs a step recorded late or dropped is
dropped and recorded late. Their records go to the `finished` callback; the
tick site passes one that prints their actions, then
`slow lane step <name> failed: <error>` for each failed step and
`a slow lane step held <repr>` when the pass box holds a held exception, printed
with its repr. When the
pass ends, `scripts.tick.pool.running` joins each handed off thread for at
most the slow lane time left plus `LATER_GRACE_S` (one second), one thread
after another, so each extra hung thread adds about one second. A step that
needs a step an earlier group handed off waits for it as
[Needs across groups](#needs-across-groups) describes. In sequential mode every step runs
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
    needs=frozenset({"spawn"}),
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

#### Needs across groups

A step's `needs` may name a step of an earlier group. The tick runs its groups
in order, so in sequential mode the needed step has already ended when the
dependent starts. In a parallel pass the earlier group returns before its slow
lane steps end: they run on as handed off steps. A step that needs one of them
waits for it, then runs or is dropped by how it ended.

The tick's own example: `metrics record`, in group `wrapper metrics`, needs
`health findings`, a slow lane step of group `wrapper events`. It starts only
after `health findings` ended or was held:

```python
CallStep(
    "metrics record",
    "wrapper metrics",
    held(viewed("metrics record", _metrics)),
    FOUND,
    pass_domains("found", "view") | hook_domains("doc", "count"),
    lane=SLOW,
)
```

`FOUND` in `scripts.tick.steps.report` is `frozenset({"health findings"})`.

Turn parallel mode on, or back off, per swarm:

```bash
agentihooks swarm <slug> set tick-mode=parallel
agentihooks swarm <slug> set tick-mode=sequential
```

How the needed step ended decides the dependent:

| Needed step, from an earlier group | Dependent |
| --- | --- |
| Still running when the dependent's group starts | Handed off with it, even when the dependent is critical; it keeps its own lane deadline and starts once the needed step ends |
| Ended `ok` or `failed` | Runs |
| Recorded `held`: its earlier run was still alive | Runs |
| Recorded `skipped`: its interval had not elapsed | Runs |
| Left running at its budget (`late`), cut at its lane deadline, or dropped before it started | Dropped and recorded `late`, without running |
| Not ended before the dependent's lane deadline | Dropped and recorded `late`, without running |
| Ran critical in its own group | Already ended; the need is met at once |

A dropped dependent drops every step of its group that needs it and, when it
was handed off, every step of a later group that needs it. When a group
returns, its handed off steps that need a step of the group that was dropped
are marked ended and dropped at once, so a later group does not wait on them.

A kept step (`kept=True`), such as `status publish`, waits at most its own
budget for the steps it needs and runs even when one of them was dropped,
unless the pass ended before it started.

While it waits, the dependent holds no pass worker; see the last paragraphs of
[Pass executors](#pass-executors). Each handed off step is tracked on the
active `scripts.tick.pool.Pass`:

| Method | Returns or does |
| --- | --- |
| `hand(names)` | Marks each step handed off and pending |
| `pending()` | The handed off steps not yet ended. The next group hands off every step that needs one of them |
| `handed_of(names)` | The steps in `names` that a group of this pass handed off |
| `settled(names)` | Whether every step in `names` has ended, dropped or not |
| `end(name, dropped)` | Marks a handed off step ended, dropped when its record was lost; ignores a step never handed off |
| `ended(names, timeout)` | Whether every step in `names` ended within `timeout` seconds and none was dropped |

A test that holds the dependent back until the step it needs ends, after
`tests/tick/test_cross_group_needs.py`:

```python
from threading import Event

from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Result
from tests.tick.test_cross_group_needs import Step, View

ctx = Context(View(), Deadline.after(10.0))
release, started = Event(), Event()


def find(ctx: Context) -> Result:
    release.wait(5.0)
    return Result()


def record_metrics(ctx: Context) -> Result:
    started.set()
    return Result()


findings = Step("findings", find, "events")
record = Step("record", record_metrics, "metrics", frozenset({"findings"}))
with pool.running(workers=4, slow_s=3.0):
    engine.run_group("events", ctx, (findings,))
    engine.run_group("metrics", ctx, (record,))
    assert not started.wait(0.2)
    release.set()
assert started.is_set()
```

`Step` and `View` are the test's doubles; `Step` is slow lane by default. A
sequential pass runs the same two groups in order with no handoff.

| Symptom | Cause | Check |
| --- | --- | --- |
| A step records `late` with no wall time in a parallel pass | A step it needs, in its group or an earlier one, was dropped, ran past its budget or lane, or did not end before the dependent's lane deadline | Read the needed step's record in the same pass; `agentihooks swarm <slug> tick-report` lists both |
| A critical step's actions print with the slow lane steps | It needs a step an earlier group handed off that was still running, so it was handed off with it | Expected; its critical deadline still applies |
| A dependent ran although the step it needs failed | A failed step counts as ended | Guard the dependent's body on what the failed step should have written |

### Pass executors

A parallel pass owns two thread pools, built once by `scripts.tick.pool.running`
and shared by every group and every fan out of that pass:

| Field | Pool | Threads | Runs |
| --- | --- | --- | --- |
| `Pass.steps` | Step executor | Up to `workers`, named `tick-step_<n>` | Every eager step of every group, critical and handed off slow lane steps alike |
| `Pass.fans` | Fan out executor | Up to `workers`, named `tick-fan-out_<n>` | Every item of every `scripts.tick.pool.fan_out` call |

`workers` defaults to `scripts.tick.pool.WORKERS`, 8; `pool.running(workers=...)`
sets it for one pass and refuses a value below one. No setting or environment
variable changes it. Handed off slow lane work still runs from one daemon thread
per `run_group` or `run_graph` call that hands work off, named `tick-slow-lane`, which submits its steps to the same
step executor. A pass of three groups of eight steps, each step fanning out over
four items, therefore uses at most `2 × workers` pool threads plus one
`tick-slow-lane` thread for each group that hands work off. Before this change
each group and each fan out built and shut down a thread pool of its own.

A fan out holds one of the pass's `workers` slots (`Pass.slots`) for each item
it runs, so all fan outs of a pass together run at most `workers` items at once.
A fan out started inside a fanned out action runs its items inline on the
calling thread. A sequential pass, a pass-free call and a fan out over fewer than
two items run inline as well, and none of them touches the executors.

When the pass ends, `pool.running` joins the `tick-slow-lane` threads as
described above, then shuts both executors down without waiting and cancels
every future still queued on them. A step or fan out item already running is
not interrupted: its daemon worker finishes in the background, as an abandoned
step does. A submit after the pass ended raises `RuntimeError`. When one group or
one fan out returns, it cancels only its own futures that never started; the
executors stay open for the rest of the pass.

A step that needs a step an earlier group handed off is not submitted until that
step ends or is dropped (`Pass.settled`). While it waits, `_eager` holds it out
of the ready set (`engine._waiting`) and re-checks every `SETTLE_S` (0.05
seconds), so the wait
holds no pass worker and the slow step it waits on can always get one. The hold
lasts only while the needed step is still pending and the waiting step's own
lane deadline has not expired; once either ends, the step is submitted and the
usual drop rules apply.

`run_group` orders a group through `registry.ordered`, which refuses a cycle
among the group's steps before any step runs:

```text
ValueError: Dependency cycle: a, b
```

`_eager` keeps its own guard for steps handed to it without that ordering:
when nothing runs, nothing is held and no pending step is ready, it raises on
the first idle round instead of looping:

```text
ValueError: steps ['a', 'b'] wait on needs that never end
```

Read the executors of the active pass from a step or a test:

```python
from scripts.tick import pool

with pool.running(workers=3) as found:
    assert found.steps.submit(lambda: 1).result() == 1
    assert found.fans.submit(lambda: 2).result() == 2
    pool.fan_out(print, range(4))
```

### The closing lane

`last tick`, `heartbeat` and `status publish` run in the `closing` group,
which `run_tick` calls from the `finally` of the pass, after every other group
and whether the pass completed, ended early or raised. The three steps declare
lane `closing` (`scripts.tick.steps.report.CLOSING`), so in a parallel pass
they run against a deadline of their own instead of the critical deadline that
may already have expired:

| Value | Meaning |
| --- | --- |
| `scripts.tick.pool.CLOSING_LANE` | The lane name, `closing`. |
| `scripts.tick.pool.CLOSING_S` | 30 seconds, the closing lane deadline. `pool.running(closing_s=...)` sets it for one pass; no setting or environment variable changes it. |
| `Pass.close()` | Starts the closing deadline on its first call and returns that same deadline on every later call. `Pass.lane("closing")` calls it, so the deadline starts when the first closing step looks up its lane, at the start of the `finally`. |

A closing step is not a slow step: the group runs it eagerly and returns its
record before the pass ends. Its context deadline is the smaller of the group
budget and the closing deadline, and its own step budget still applies. When
the pass ends, `pool.running` joins each handed off slow lane thread for the
larger of the slow lane time left and the closing lane time left, plus
`LATER_GRACE_S`. A sequential pass looks up no lane deadline, so the closing
lane changes nothing there; the three steps run in group order with their
budgets.

`run_tick` hands the group an `ended` value that says how the pass ended:

| How the pass ended | `ended` | `last tick` | `heartbeat` | `status publish` |
| --- | --- | --- | --- | --- |
| Completed its groups | `full` (`report.FULL`) | Writes the current time to `<slug>/last-tick` | Writes the [tick heartbeat](../pillars/swarm.md#tick-heartbeat-and-stale-watcher) and clears the early end streak | Publishes the tick status |
| No step stored a value a later step needed (`Left`) | `early` (`report.EARLY`) | Writes the current time | Writes the tick heartbeat; the streak keeps the count `early_end` added | Publishes the tick status |
| Returned before the end (stopped, binned, refused) or raised | none | Writes nothing | Writes nothing | Publishes the tick status, with the exception as `failure` when one was raised |

`report_steps.early_end` no longer stamps the pass itself. It counts the
missing value in the early end streak and returns the
`pass ended early: no step stored <value>, the pass left <steps>` line; the
closing group stamps last tick and the heartbeat afterwards.

The closing lane exists for a pass whose critical lane ran out. Before it, the
three steps sat in the `wrapper metrics`, `after` and `wrapper status` groups
on the critical lane, so a pass whose critical lane ran out dropped
them and wrote no heartbeat, and the stale watcher then judged a live tick
stale. Now a pass with a hung step still stamps and publishes: in
`tests/tick/test_closing_lane.py` a reboot pass planted to hang for 300
seconds under a 1 second critical lane ends early, and the closing group
records `heartbeat`, `last tick` and `status publish` as `ok` with last tick
and the heartbeat written.

A new step joins the closing lane only when it must run on every pass ending,
after the other groups:

```python
from scripts.tick.steps.report import CLOSING, CLOSING_GROUP

CallStep("example", CLOSING_GROUP, held(_example), lane=CLOSING)
```

Read `get("ended")` in its action to tell the endings apart; it is `None` when
the pass raised or returned before its end.

### Spawn prerequisites and the forty second bound

Spawn declares `budgets.SPAWN_NEEDS` as its prerequisites:

- The judge steps (`budgets.JUDGE`): `verify`, `reap`, `launch checks`,
  `track outages`, `overrun pass` and `quiet pass`.
- `drained resume`, `recover outages` and `prerequisite reopen`.

`registry.latest_start("spawn", steps)` follows the longest prerequisite
path and sums the declared budgets on that path. The delivered registry
computes 28 seconds, below `SPAWN_START_S` of 40 seconds. This bounds when
spawn can start once execution follows prerequisites and each prerequisite
finishes or is left behind by its budget; it does not bound spawn completion.
Spawn itself has a 33 second budget.

The flat pass graph keeps a barrier between each site and the next (see
[The flat pass graph](#the-flat-pass-graph)), so unrelated earlier sites can
still add waiting time. Forty seconds is a prerequisite graph bound, not a
measured wall time guarantee for the pass.
The existing spawn conditions and placing lock continue to apply.

### The flat pass graph

`engine.run_graph(steps, ctx, apply=None, finished=None, gate=None)` runs
every step of `steps` as one graph. It takes the same `apply` and `finished`
arguments as `run_group` and returns one `StepRecord` per step in graph order:

- Outside a parallel pass, and in sequential mode, the steps run one at a time
  in graph order, each in its own daemon thread the caller waits on up to its
  budget.
- In a parallel pass each step is submitted the moment every step it needs
  among them has ended. Slow lane steps, and every step that needs one of them,
  are handed off as `run_group` hands them off and their records go to
  `finished`.

Graph order comes from `registry.ordered(steps)`: each step after the steps
among them it needs, otherwise in the order given. A need on a step outside
the list is ignored for ordering. A cycle raises
`ValueError: Dependency cycle: <names>`.

#### Site edges

A group site in the pass is a barrier: no step of a site starts before every
step of the previous site ended. `registry.site_edges(steps, sites, joins=None)`
turns that order into edges. `sites` is the group names in pass order. The
returned steps keep their order, name, group, lane, budget and action; only
`needs` changes:

- Each step of a site gains a need on every step of the nearest earlier site
  that holds steps. An empty site is skipped.
- A step drops any need on a step of a later site, so a later site never holds
  an earlier one.
- A slow lane step, and every step that needs one directly or through another,
  is no barrier for a later site: later sites do not wait for it.
- A site that `joins` maps to a later site is no barrier for the sites between
  them. Only the steps of the site it names need it.
- A step whose group is not in `sites` keeps its own needs.

With steps `a1` and `a2` in group `a`, `b1` in `b` needing `a1`, and `c1` in
`c`, `site_edges(steps, ("a", "b", "c"))` gives `b1` the needs `a1` and `a2`
and `c1` the need `b1`. With sites `("a", "empty", "b")`, `b1` still needs `a1` and `a2`: the
empty site changes nothing, and `c1`, outside the sites, keeps no needs.

#### Site gates

The `gate` argument is called with each step as it becomes ready and the
records of the steps that ended so far. A step it shuts is recorded rested: it
starts no thread and ends at once for the steps that need it, as a step resting
inside its interval does. `engine.SiteGate(hooks, shut)` is the gate the tick
uses:

- `hooks` maps a site to a check run once, when the first step of that site is
  ready. A check that returns false shuts every step of the site.
- `shut(step)` is asked before and after the hook and shuts single steps.
- `seen` collects the records of every step that ended before the latest step
  became ready.

A site's hook runs when the site's first step is ready. A site whose steps all
run in the slow lane is no barrier, so later sites can start before its hook
runs. A hook that sets pass state later steps read, such as the runtime fence,
belongs on a site that holds a critical step; `run_tick` fences the runtime at
`wrapper probes` for that reason.

```python
from scripts.tick import engine, registry

gate = engine.SiteGate({"observe": lambda: config_loaded()}, lambda step: stopped())
edged = registry.site_edges(steps, ("start", "pass setup", "observe"))
records = engine.run_graph(edged, ctx, apply=appliers, finished=print_later, gate=gate)
```

#### How the tick runs its pass

`scripts.tick.sites.run_sites(sites, state, box, gate, apply, joins=None)`
takes every registered step whose group is in `sites`, sorts it by site, adds
the site edges and runs the result through `run_graph` under one deadline of
`GROUP_BUDGET_S` (600 seconds). Handed off steps print their actions through
the tick's `_print_actions`. Steps read the pass state through a view that
answers `box`, then `sleeping` (from the box), then a box value, then the pass
state.

| Caller | Sites | Gate |
| --- | --- | --- |
| `scripts.swarm.tick.tick` | `scripts.tick.sites.TICK_SITES`, the seventeen sites of the [table of groups](#groups-and-their-sites) | `TickSites` hooks and `shut` |
| `scripts.swarm.cli.run_tick` | `PASS_SITES`: the wrapper sites `wrapper probes` through `wrapper reboot`, then `TICK_SITES`, then `wrapper relay`, `wrapper view`, `wrapper events` and `wrapper metrics`, with `INTENT_JOINS` joining `wrapper intent` to `wrapper relay` | The wrapper's and the tick's hooks, the wrapper's `shut` |

`run_tick` still runs the `before` group and the `closing` group through
`run_group`, before and after the graph. The checks the hand written pass ran
between groups are now site hooks and `shut` rules. The table lists every site
of `run_tick` in pass order; `tick` runs only the tick sites and their hooks.
Tick sites not listed have no hook.

| Order in `run_tick` | Site | Hook |
| --- | --- | --- |
| 1 | `wrapper probes` | Fences the runtime the rest of the pass uses |
| 2 | `wrapper health` | Holds the pass's ledger view |
| 3 | `wrapper commands` | Starts the spawn stall watch, closed when the graph returns |
| 4 | `wrapper phases` | Ends the pass when the ledger is binned or gone, else reads its pass state and clears the halt marks |
| 5 | `wrapper refresh` | None |
| 6 | `wrapper intent` | Reads the pass state and config the intent check judges against |
| 7 | `wrapper reboot` | None |
| 8 | `start` (tick) | Loads the master move modules, starts the pass process scan in `run_tick`, sets `now_ms` and `due`, adds the task appliers |
| 11 | `agents` (tick) | Shuts the site and every later tick site when observation left `config`, `doc` or `rows` out of the box |
| 14 | `tasks` (tick) | Shuts the site and every later tick site when the drained resume left `config` out of the box |
| 25 | `wrapper relay` | Ends the tick's process scan, raises what the intent check held and keeps its view and checked actions |
| 26 | `wrapper view` | Reads the pass state, config and task rows again for the view, events and metrics sites |
| 27 | `wrapper events` | None |
| 28 | `wrapper metrics` | Hands metrics the number of actions the pass counted so far |

`wrapper intent` joins `wrapper relay`: the sites from `wrapper reboot` through
the tick sites do not wait for it, and only `wrapper relay` does.

`TickSites.shut` shuts every step once a step held an exception in the box,
every tick site after `start` once the stopped branch finished the pass or a
value the tick reads was left out, and
the `master seats` and `placement` sites while the swarm sleeps. The wrapper's
`shut` also shuts every site after `wrapper probes` once the ledger health probe
stored `stop`, and every site once the pass ended because the ledger was
binned or gone. After the graph, the held exception is raised as before, and
`tick` returns no actions when a value it reads was left out.

The `masters.run_site` runner and its per site box are gone; the master sites
share the pass box. Placement no longer holds the host placement lock around
its sites: each step of `placement notices` (quota notice), `placement acks`,
`master seats`, `dispatch seat` and `placement` (every step but
`bottleneck remedy`) takes `tick.PLACING` in its own
body through `scripts.tick.steps.tasks.placing`.

## Pass state

The tick builds one state dictionary at the start of the pass and adds the
ledger values after the `observe` group reads them. Every group site receives it:

| Key | Value |
| --- | --- |
| `slug`, `store`, `ledger`, `runtime` | The swarm and its collaborators |
| `rows` | The ledger tasks keyed by id, from the `observe` group |
| `doc` | The ledger state, from the `observe` group |
| `now_ms` | The pass time |
| `due` | The Redis key of the swarm's `spawn-due` marker, added by the `start` site hook |
| `box` | The exception holder described under [Exceptions](#exceptions), shared by every site of the pass graph |
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
| `config` | `config load`, then `stopped branch` and `drained resume` | `master wake`, `master stall`, `strays`, placement notices |
| `finished` | `stopped branch` | The `start` group site in `scripts.swarm.tick`, which ends the pass |
| `doc`, `rows` | `ledger state`; `spawn` rewrites `rows` | `automated cleanup`, `launch checks`, `sleeping`, `spawn` |
| `sleeping` | `sleeping` | `drained resume`, `dispatch seat`, `night watch`, `codex hook order` |
| `agents` | `page relay` | `stall watch`, `inbox wake` |
| `view` | `tick view` | The slow lane event steps, `health findings`, `metrics record`, `status publish` |
| `live` | `stall watch` | `overrun pass` |
| `found` | `health findings` | `findings pass`, `metrics record`, `status publish` |
| `judged`, `stop` | `intent check`, `ledger health` | No step; `stop` is read by `scripts.swarm.cli.run_tick` |

Because the readers of `config` need `drained resume`, that step carries
`DRAINED_RESUME_NEEDS` (`scripts.tick.steps.tasks`) plus `sleeping`: every step
that reads `config` before it, so the rewritten config is ordered after them.
`spawn` needs `drained resume` directly, through `budgets.SPAWN_NEEDS`, so it
reads the rewritten config.

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
`WriteStep` takes the same `needs` and `reads` as keywords. Both also take
`effects`, the ports the step may write through, and `feature`, its feature map
id, as keywords; see
[Declaring effects on a step](tick-adapters.md#declaring-effects-on-a-step).

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

The groups run in this order in one pass, each as a site of the
[flat pass graph](#the-flat-pass-graph). `run_tick` adds the wrapper sites
around them. Groups that hold the task, planning
and placement steps are in bold; the others hold steps from other changes.

| Order | Group | Site in the pass | Steps |
| --- | --- | --- | --- |
| 1 | `start` | First line of the pass | config load, stopped branch |
| 2 | `pass setup` | After `start`, unless the stopped branch ended the pass | spawn hold clear, pass budget |
| 3 | `observe` | After `pass setup` | pane evidence through verify |
| 4 | `agents` | After the observation sweeps | reap, recover outages, strays, failed panes |
| 5 | `masters` | After the agents | master wake, retire idle master, master stall, prompt stall |
| 6 | `drained resume` | After the masters | sleeping, drained resume |
| 7 | `tasks` | After `drained resume` | none; kept for its site hook |
| 8 | `reopen passes` | After `tasks` | dev red reopen, prerequisite reopen, promotion refresh, release alert, [fleet red notice](fleet-red-notice.md) (slow lane) |
| 9 | **`task passes`** | After `reopen passes` | run window, verification notices, size pass, grouping release, dispatcher group |
| 10 | `place` | First line inside the placing lock | |
| 11 | `placement notices` | After `place`, inside the placing lock | quota notice, lost runner (slow lane), time left (slow lane) |
| 12 | **`placement acks`** | After `placement notices`, inside the placing lock | codex hook order |
| 13 | `master seats` | Inside the placing lock, only when the swarm is not sleeping | master down, tick master, master scale, seat alarm |
| 14 | `dispatch seat` | After the master seats, inside the placing lock, ahead of lane placement so a pending dispatcher trigger takes the next free hive slot | dispatch seat |
| 15 | **`placement`** | Last site inside the placing lock, only when the swarm is not sleeping | rank pass, bottleneck remedy, spawn |
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

| Step | Call |
| --- | --- |
| run window | `run_window.step` |
| verification notices | `claim_hold.verification_notices` |
| size pass | `tasks.size_writes` returns a `sizes` task write per task `difficulty.sizes` sizes, applied by `tasks.task_writer` only while the task is still unsized |
| grouping release | `tasks.release_writes` returns a `groups` write per lead `grouping.releases` finds stopped, applied by `tasks.group_writer` only while the lead still holds the members it read |
| dispatcher group | `dispatcher.group` |

### Placement (`scripts.tick.steps.place`)

| Group | Step | Call and condition |
| --- | --- | --- |
| `placement acks` | codex hook order | `_codex_hook_order`, skipped while the swarm sleeps |
| `placement` | rank pass | `tasks.rank_writes` returns `dispatcher.rank_decision` as one `rank` write; the rank applier takes it with `dispatcher.apply_ranking` only while the saved claim queue heads it read still hold, else drops it for the next pass |
| `placement` | bottleneck remedy | `bottleneck_remedy.step` |
| `placement` | spawn | `_spawn` under the placement pass, only when the swarm is `running`, `readiness.tick_launches` allows the engine to launch (never under events) and no panic is set; the step also deletes the `spawn-due` marker on that path |

The events engine is now the only swarm engine, so `readiness.tick_launches`
returns false and the whole guarded body of `_spawn_step`, the launch and the
marker deletion alike, is skipped every pass: the step returns no actions.
The deletion call stays in its source for the path where `tick_launches` is
true. The marker key is still the `due` value the `start` site hook names,
but nothing in the pass sets that marker any more: the `spawn due` step that
used to set it is gone.

### Close (`scripts.tick.steps.tasks`)

| Step | Call |
| --- | --- |
| settle | `_settle` |
| close space | `_close_space` |

The agent steps in `close` (conversations, session models, handoff transfers) run before them. A stopped swarm
that is not woken ends the pass in the `stopped branch` step of the `start`
group, which makes its own `_close_space` call, and never reaches the `close`
group.

## Shared helpers (`scripts.tick.steps.common`)

The helpers that the step modules and the tick module both call live in
`scripts.tick.steps.common`. They moved there from `scripts.swarm.tick`
with the same behavior, their references to tick module names turned into
`tick.<name>` lookups, in
[pull request 9708](https://github.com/the-cloud-clockwork/agentihooks/pull/9708),
the first sweep of top level definitions out of the tick module. The tick
module imports each one back, so `scripts.swarm.tick.<name>` and
`scripts.tick.steps.common.<name>` are the same function object and every
existing caller, such as `tick.skip_refused(...)` in a step module, keeps
working with no import change.

| Helper | What it does |
| --- | --- |
| `skip_refused(function, *args)` | Calls `function` through `timing.call`. A `LedgerRefused` or a Redis read timeout prints the reason to stderr and returns the single action `skipped <module>.<name>: the ledger refused its write` or `skipped <module>.<name>: its Redis read timed out` instead of raising |
| `skip_dropped(function, *args)` | Calls `function` through `timing.call` up to twice on a Redis connection drop or timeout, returning its actions or `[]`. Two failures return `skipped <module>.<name>: its Redis connection dropped or timed out twice` |
| `_operator_line(item)` | True for an inbox item from `operator` that is not an fyi control notice (a ref starting with `control_notifications.CONTROL_REF`); the tick wakes a swarm on such a line |
| `_drop(slug, store, ledger, rows, agent, refund=False)` | Releases the agent's claim and drops the agent. While the agent still holds an active task that is not awaiting merge, it reopens the task guarded on the claimant and claim generation it read, and with `refund=True` gives back the held life when the task has a claim to refund and the ledger row still matches. Settles the agent's inbox as `stopped`, moving its items to the seat only when the task reopened. Returns `, task <id> reopened` or an empty string |
| `_collected(slug, store, ledger, rows, agent)` | For a master, saves the task ids of `rows` under the swarm's `master-retired-tasks` key, then calls `_drop` |
| `_still_held(row, expected)` | True when the row is active and its `claimed_by` and `claim_generation` (missing counts as 0) match `expected`; a missing row is not held |
| `_follow(slug, store, runtime, agent, pid)` | Returns an `(agent, actions)` tuple. With no process id: `(agent, [])`. Otherwise it calls `_rebind`: when that finds the pane, `(rebound agent, ["rebound <agent> to its resumed process <pid> in pane <pane>"])`; when it does not, `(None, ["held <agent> until one pane holds its resumed process <pid>"])` |
| `_rebind(slug, store, runtime, agent, pid)` | Finds the panes whose conversation is the agent's, ignoring pane ids that contain a slash. With exactly one, stores the agent on that pane with `pid` in its profile validation and returns it; otherwise returns `None` |
| `_ended(agent, rows)` | True for an agent in state `finished`, or for a non master agent whose task row has `done` set, is in state `done`, `blocked` or `handoff`, or awaits verify with no claimant |
| `_current_owner(agent, row)` | True when the row is unclaimed or claimed by this agent |
| `_reopen(slug, ledger, rows, task_id, expected=None)` | Sets the task `open` and unclaimed only while it is active, and, with `expected`, only while its claimant and generation still match. Updates `rows[task_id]` with the live row the ledger returns, so `task_id` must be a key of `rows`, and reports whether it is now `open` |

### How the helpers reach the tick module

`scripts.swarm.tick` imports the common module at its top, so the common
module cannot import the tick module at its own top. It binds `tick` with
`import_module("scripts.swarm.tick")` on its last line instead, and every
helper looks up what it uses on that module when it runs: `tick.timing`,
`tick.LedgerRefused`, `tick.ACTIVE`, `tick.MASTER`, `tick.awaiting_merge`,
`tick.exits`, `tick.InboxStore` and `tick.control_notifications`.

A helper that calls a sibling helper calls it through the tick module as well:
`_collected` calls `tick._drop`, `_drop` calls `tick._still_held` and
`tick._reopen`, and `_follow` calls `tick._rebind`. A test that patches the
name on `scripts.swarm.tick` therefore reaches every caller, including the
moved helpers:

```python
from types import SimpleNamespace

import pytest

from scripts.swarm import tick
from scripts.tick.steps import common


def test_follow_holds_when_no_pane_rebinds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick, "_rebind", lambda *args: None)
    agent = SimpleNamespace(name="engineer@1-1")
    assert common._follow("slug", "store", "runtime", agent, 7) == (
        None,
        ["held engineer@1-1 until one pane holds its resumed process 7"],
    )
```

Patch the tick module, never `scripts.tick.steps.common`: the helpers do not
read their siblings from the common module, so a patch there changes nothing
that runs in a pass.

### Adding or moving a shared helper

1. Move the function into `scripts/tick/steps/common.py` with the same
   behavior, add full type annotations, and replace every bare reference to a tick module
   name with `tick.<name>`, including sibling helpers.
2. Add the name to the `from scripts.tick.steps.common import (...)` block in
   `scripts/swarm/tick.py`. Mark a name the tick module no longer calls itself
   with `# noqa: F401`; it stays imported so `tick.<name>` and test patches
   keep working.
3. Run `python -m scripts.tick_pattern --write` and commit the shrunk
   `tests/TICK_PATTERN_ALLOWLIST.json` with the move. The
   [tick pattern gate](tick-pattern-gate.md) records each top level `def` in
   the tick module, so every moved helper removes its `def <name>` line
   from the tick module's list; pull request 9708 removed eleven.
4. Add a test to `tests/tick/steps/test_common.py` under the
   `swarm-tick-common-helpers` feature and list it in that entry of
   `feature-map.json`.

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
| `expected` | The guard: what the step read. Defaults to `None`, which the size applier reads as no fields to compare and the group applier as an expected empty member list |

These steps return writes. Every domain constant except `fleet_red.LISTINGS`
lives in `scripts.tick.steps.tasks`.

| Step (group) | Domain | Key | Guard (`expected`) | Applier and what it does on a match |
| --- | --- | --- | --- | --- |
| size pass (`task passes`) | `sizes` | task id | `{"difficulty": ""}`: the task is still unsized | `task_writer`: `ledger.update_task` with the guard as `if_match` |
| grouping release (`task passes`) | `groups` | lead task id | `{"group_members": [...]}`: the lead still holds the members the step read | `group_writer`: `ledger.ungroup_tasks` with the guard as `if_match` |
| fleet red notice (`reopen passes`, slow lane) | `fleet red` | task id | `{"state": "open", "pr_url": url, "claimed_by": ""}` | `fleet_red_writer`: posts the hold notice only while no seat claimed the task |
| fleet red notice | `fleet red lifted` | swarm | none | `lifted_writer`: deletes the hold marks the write names, one per line |
| fleet red notice | `fleet red listings` | listing key | no listing saved under the key (Redis `nx`) | `fleet_red.saver`: saves the open pull request listing for ten minutes; a Redis timeout also drops it |
| rank pass (`placement`) | `rank` | swarm | the saved claim queue heads the pass read | `rank_writer`: `dispatcher.apply_ranking`; a pass that lands no heads only proposes ranks and takes no guard |
| planning and phase pass (`wrapper phases`) | `phases` | phase id | `not change.done`: the done state the step read | `phase_writer`: marks the pass box and sets the checkbox through `phases.apply_change` |
| master stall (`masters`) | `master stall` | master name | the master name the step read | `stall_writer`: `master_stall.apply_stall` while that master is still the live master |

Each step reads the pass state and leaves it as read. `difficulty.sizes(doc)`
yields each unsized open task's fields (at most `PER_TICK`, five, per pass);
`grouping.releases(slug, store, doc)` returns each stopped lead with the reason
and its members; `fleet_red.holds` returns new holds, lifted marks and listing
saves; `dispatcher.rank_decision` returns one `Ranking`;
`phases.phase_changes(doc)` returns `PhaseChange` items projected from the pass
document with no ledger read (see
[One ledger read per pass](tick-ledger-view.md#one-ledger-read-per-pass));
`master_stall.stalls` returns `Stall` items.

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
   the task the server returned and treats a guarded field the update also
   writes, which does not hold the written value, as moved.
4. Each of these raises `RevisionConflict`: a guarded value that moved, a
   ledger call the task, group or phase applier finds refused
   (`LedgerRefused`), a fleet red task a seat claimed since the read
   (`_post_hold`), a failed fleet red notice, a listing another pass already
   saved, or a write whose value is not the expected type. The rank and stall
   appliers do not catch `LedgerRefused`. A failed fleet red notice first
   deletes its hold mark, so the next pass posts it again.
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

### The server guard on size and group writes

The held view compare in step 2 sees the ledger as of the pass's latest read,
so a write another process lands between that read and the call would still
pass it. The size and group appliers close that window by sending the guard
with the call, and the ledger server compares it inside the same locked apply
that makes the change.

| Applier | Ledger call | Operation sent | Server compare | On a mismatch |
| --- | --- | --- | --- | --- |
| `task_writer` (`sizes`) | `LedgerClient.update_task(slug, key, fields, if_match=expected or None)` | `task_update` with `fields` and `if_match` | each `if_match` field against the task, a missing field read as its default (`""` for `difficulty`) | the update is skipped, not refused; the call returns the task as it stands |
| `group_writer` (`groups`) | `LedgerClient.ungroup_tasks(slug, key, if_match={"group_members": [...]})` | `task_ungroup` with `if_match` | `group_members` of the lead against the expected list, order included | the operation is refused with `tasks/<lead> changed its group members` |

A write with no `expected` fields sends no `if_match` to `update_task`, and
the server then applies it unguarded. The group applier always sends one; a
write with no `expected` sends an empty member list. When the held view shows
the lead with members, the applier's own compare raises `RevisionConflict`
before any ledger call; the server refusal of a lead holding members covers a
stale view.

The server accepts these `task_update` guard fields, each compared against its
default when the task lacks it: `claimed_by`, `pr_url`, `awaiting` and
`difficulty` (`""`), `claim_generation` (`0`) and `group_members` (`[]`). Any
other field is rejected with `if_match must contain task ownership fields`.

Because a guarded `task_update` that misses is skipped without a refusal, the
size applier checks the task the call returns: a field that is both guarded
and written, and does not hold the written value, raises `RevisionConflict`
with `task <id> changed <fields> on the ledger before the write`. A refused
`task_ungroup` reaches the group applier as `LedgerRefused`, which it raises as
`RevisionConflict` with `the ledger refused the release of the group under task
<lead>`. Both writes are dropped and recomputed like any other.

#### `task_ungroup` with a guard

The operation takes `op`, `id`, `by`, `item` (`tasks/<lead id>`) and an
optional `if_match`. `if_match` holds exactly one key, `group_members`, a list
of task ids; an empty list is valid. Without `if_match` the operation releases
whatever group the lead holds, as before.

```json
{
  "op": "task_ungroup",
  "id": "task_ungroup-1",
  "by": "swarm",
  "item": "tasks/t1",
  "if_match": {"group_members": ["t2", "t3"]}
}
```

From Python, through the swarm's ledger client:

```python
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused

try:
    LedgerClient().ungroup_tasks("example-swarm", "t1", if_match={"group_members": ["t2", "t3"]})
except LedgerRefused as exc:
    print(exc)
```

The server answers each case this way:

| Request | Result |
| --- | --- |
| The lead holds `["t2", "t3"]` and the guard expects `["t2", "t3"]` | Released: the lead loses `group_members`, each member whose `merged_into` names the lead loses it, and an `ungrouped` event lists the released members |
| The lead holds `["t2", "t3"]` and the guard expects `["t2"]`, or `["t3", "t2"]` | Refused: `tasks/t1 changed its group members`; the group is unchanged |
| The lead holds no group | Refused: `tasks/t1 leads no group`, checked before the guard |
| `by` is an agent of the `eng`, `ci` or `plan` lane | Refused: `<name> works in the <lane> lane and cannot release a task group` |
| `by` is missing, not an agent name, or `operator` | Rejected before apply: ``task_ungroup needs `by`, an agent name other than operator``, checked first |
| `if_match` is not an object, holds a key other than `group_members`, or `group_members` is not a list of task ids | Rejected before apply: `task_ungroup if_match holds only group_members, a list of task ids` |
| Any key other than `op`, `id`, `by`, `item` and `if_match` | Rejected before apply: `task_ungroup takes only an id, by, an item tasks/<lead id> and an if_match` |

The API schema lists `if_match` among the `task_ungroup` fields, so the HTTP
ledger API accepts the same body. `task_group` takes no guard.

The guard is covered by `tests/swarm_ledger/test_task_group.py` (release on a
match, refusal after a concurrent regroup, malformed guards),
`tests/swarm/test_ledger_client_group.py` (the client sends the guard) and
`tests/tick/test_apply.py` (a size and a release a concurrent write beat on
the server are dropped).

### Dropped writes and the recompute

A dropped write is not retried in the same pass. `steps.actions(records)`
returns, per step in order, its own actions, then the action lines its appliers
returned, then one line per write that step dropped:

```text
dropped write sizes t7 of step size pass: task t7 changed difficulty since the step read it; the next pass recomputes it
dropped write groups t3 of step grouping release: task t3 changed group_members since the step read it; the next pass recomputes it
dropped write sizes t7 of step size pass: the ledger refused the write of task t7; the next pass recomputes it
dropped write sizes t7 of step size pass: task t7 changed difficulty on the ledger before the write; the next pass recomputes it
dropped write groups t3 of step grouping release: the ledger refused the release of the group under task t3; the next pass recomputes it
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
parallel pass, with a test step named `size` that writes a test domain `tasks`
guarded on a revision number:

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
other writers of that domain. `waves()` refuses two writers of one domain with
no dependency between them, so the example, which writes `SIZES` beside the
size pass, needs it:

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


registry.register(
    WriteStep("example", TASK_PASSES, held(_example), writes=frozenset({SIZES}), needs=frozenset({"size pass"}))
)
```

A write to a domain with no applier at the site is neither applied nor dropped:
it stays on the record's result and changes nothing. A new domain needs its
applier added to `tasks.appliers`. The step name still needs its timing entry,
as described above.

### Limits

- The held view compare sees only changes up to the view's latest refresh.
  Without a held view the task, group, fleet red and phase appliers skip that
  compare; a task the view lacks compares as empty, and a phase it lacks as
  not done. The size and group writes are still compared on the server
  through `if_match`; the fleet red, phase, rank and stall writes are not.
- A dropped write waits a whole pass for its recompute.
- `LedgerRefused` in the task, group or phase applier is a dropped write with
  a `dropped write` line, where the old in-step calls produced a
  `skipped ...: the ledger refused its write` line through `skip_refused`.
- `LedgerRefused` in the rank or stall applier is not caught by the engine and
  leaves the group run, like any other applier exception.
- An `Exception` from posting the hold notice, `LedgerRefused` included, is a
  dropped write whose line names the failed hold notice. A Redis error before
  the post, and any error from the lifted marks or listing applier other than
  a Redis timeout, is not caught and leaves the group run.

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

## Adding a step

Run the [step scaffold](tick-step-scaffold.md) from the repository root with every field the pattern needs; see
[Tick step skill and rule](tick-step-skill.md):

```bash
python -m scripts.tick.new_step "example" --group "task passes" --lane tick --interval 60 \
  --effects ledger --feature swarm-tick-example --module tasks
```

It writes five artefacts, adds a new module to `scripts.tick.steps.MODULES`, and refuses a name any of them already
holds:

| Artefact | Where |
| --- | --- |
| Step | A registered `CallStep` stub in `scripts/tick/steps/<module>.py` with every declared field; a new module joins `scripts.tick.steps.MODULES`, so the tick imports it with no new import line |
| Budget row | The step's timing row in `scripts.tick.steps.budgets` |
| Feature test | `tests/tick/steps/test_<name>.py` carrying the feature marker |
| Index row | The step's scenario in `tests/swarm/tick_scenarios/steps.json` |
| Feature entry | The `feature-map.json` entry naming the behaviour, seam and test |

`--lane` is `tick` or `slow`, `--interval` is 60, 120 or 600 seconds and
`--effects` names ports of `scripts.tick.ports.tools.Tools`. The scaffold
formats what it wrote. `python -m scripts.tick.new_step --check <name>` prints
which of the five artefacts exist and fails on a missing one.

Then fill the step body against the ports its effects name, and check the
change before pushing:

```bash
python -m scripts.tick.catalogue render
python -m scripts.tick_pattern --base origin/dev
python -m scripts.tick.catalogue check
```

`render` rewrites the [tick step catalogue](tick-step-catalogue.md); the docs
test `tests/test_docs_tick.py` is red until the committed page matches the
registry. Steps run in dependency waves (`Registry.waves`), so declare in
`needs` every step whose result the new one reads. Use `held` when an exception
from the call must still leave the pass, and add `tasks.raise_held(box)` after
the group site if the site is new.

### The feature and scenario catalogue

`python -m scripts.tick.catalogue check` imports every step module and calls
`registry.validate(catalogue)` with the catalogue `scripts.tick.catalogue.load` builds from `feature-map.json` and
`tests/swarm/tick_scenarios/steps.json`. That index maps each step name to the tick scenario names it runs in (a
golden key without its `run_tick/` or `tick/` prefix) and lists the steps still unmapped. With a catalogue the
registry refuses:

- a step whose `feature` is not a feature map id or whose name no scenario lists, unless it is unmapped;
- an unmapped entry naming a step that is now mapped or no registered step.

`python -m scripts.tick.catalogue shrink --base <base tree> --head <head tree>` refuses an unmapped step the base
index does not hold, so the unmapped list only shrinks. Production calls `validate()` without a catalogue and skips
these checks. The `Tick equivalence` workflow runs `check` on the head and `shrink` from the base tree's copy of the
module, so a head cannot loosen the ratchet, then the tick pattern grader the same way (see [The tick equivalence
run](tick-pattern-gate.md#the-tick-equivalence-run)), before the scenario replay.

The index holds two keys. `scenarios` maps a step name to the scenario names it runs in; `unmapped` lists the
registered steps that have no mapping yet. An abridged example:

```json
{
  "scenarios": {
    "example": ["dependency", "running"]
  },
  "unmapped": ["automated cleanup", "bottleneck remedy"]
}
```

A scenario name is a key of `tests/swarm/tick_scenarios/goldens.json` with its `run_tick/` or `tick/` prefix removed,
so `run_tick/dependency` and `tick/dependency` are both `dependency`. `load` refuses an index that names a scenario
with no golden, and a step with an empty scenario list counts as unlisted. A new step is mapped in the pull request
that registers it. To map an unmapped step, in one change:

1. Set its `feature` to a feature map id, adding the entry to `feature-map.json` if none fits.
2. Add its name and its scenario names to `scenarios`.
3. Remove its name from `unmapped`; `check` refuses a mapped step left there.
4. Run `python -m scripts.tick.catalogue check` from the repository root. `--root <tree>` checks another tree.

`check` and `shrink` print `<n> tick catalogue errors`, then one `::error::` line per error, and exit 1 when there is any
error, 0 otherwise. A ratchet run against a base tree without an index allows the head's unmapped list, so the first
change that adds the index passes. A head without the index passes `shrink` with an empty unmapped list, and `check`
then stops with a traceback on the missing file, so the gate stays red. `check` reports one stage at a time: an index
naming an unknown scenario is refused first, before the registry runs; then the registry's own refusals, such as
`Duplicate step name` or `Unordered writers`, each reported as that one line alone; the catalogue errors appear, all
at once, after both are fixed.

## Tests

The feature map entry `swarm-tick-step-isolation` links the measured timing,
bounded group wait and spawn prerequisite behavior to the isolation tests.
The delivered proof and limitations are recorded in
[pull request 9278](https://github.com/the-cloud-clockwork/agentihooks/pull/9278).

| Test | What it holds |
| --- | --- |
| `tests/tick/test_step_isolation.py` | The 300 second predecessor scenario, completed results, abandoned wall time and daemon worker, budget calculation, undeclared timing refusal, registered timings, spawn prerequisites and longest path start bound |
| `tests/tick/test_tasks.py` | The task passes and close steps make the old calls in order, a held exception skips the rest and is raised at the site, `fleet red notice` sits in lane `slow` with every other task step in lane `tick`, and a `WriteStep` keeps the lane it is given |
| `tests/tick/test_place.py` | Group membership and orders; `lost runner` and `time left` in lane `slow` and every other placement step in lane `tick`; `codex hook order` with and without sleep; spawn conditions for paused, events engine, panic and `tick_launches`; the `spawn-due` delete; a held exception skipping every placement step |
| `tests/tick/test_abandoned_writes.py` | A write step abandoned at its budget, and one abandoned when its lane expired, each journal one `swarm_tick_step_abandoned` line with the swarm, step, budget and sorted write domains; a call step abandoned with only its actions domain journals nothing |
| `tests/tick/test_apply.py` | The dropped write and its recompute, sequential and parallel; writes without an applier pass through; both appliers write while their guard holds, drop a moved guard and turn a refused ledger call into a conflict; appliers need a ledger and a swarm; the size pass and grouping release return guarded writes and leave the document as read; a write step keeps every attribute of its call step |
| `tests/tick/test_apply_rank.py` | The rank pass returns one write guarded on the heads it read; the applier takes it while the saved heads hold, takes a proposing pass whatever the heads, drops moved heads, and the engine keeps applied actions after the step actions and beside a later dropped write |
| `tests/tick/test_apply_phases.py` | A write per checkbox guarded on the done state read; the applier marks the box and sets the checkbox, writes through without a held view, and drops a moved, lost or refused checkbox |
| `tests/tick/test_apply_fleet_red.py` | Guarded hold writes, lifted and listing writes; the applier posts while the task stays open and unclaimed, drops a moved or claimed task, and forgets a failed notice so the next pass posts it again |
| `tests/tick/test_apply_stall.py` | The stall pass decides and writes nothing; the applier drops a stall once another master holds the seat, recomputed on the next pass |
| `tests/swarm/test_tick_groups.py` | The group sites run in the order of the table above |
| `tests/tick/test_observe.py` | The pass setup steps clear the spawn hold and update the pass budget; the start and observe steps sit in their groups in legacy order |
| `tests/tick/test_agents.py` | The agent steps in their groups, including the `close` steps before settle and close space, and the tick site printing the actions and failures of handed off slow steps |
| `tests/tick/test_pass_edges.py` | Every pass box read over the twelve tick scenarios is declared with a need on its writer and every pass box write is declared; a planted undeclared read and a planted missing need are named |
| `tests/tick/test_catalogue.py` | A step with no feature, an unknown feature or no scenario refused, an unmapped step passing, stale unmapped entries refused, a planted index drop red, a grown unmapped list red from `shrink`, and the recorded catalogue holding every registered step |
| `tests/tick/steps/test_common.py` | Each of the eleven helpers is defined in `scripts.tick.steps.common` and is the same object on `scripts.swarm.tick`; a patch on the tick module reaches `_follow`; the held, owner, ended, operator line, reopen, rebind, follow, skip, drop and collected behavior described under [Shared helpers](#shared-helpers-scriptstickstepscommon) |
| `tests/tick/test_registry.py` | Unordered writers and crossed readers refused within a lane and across lanes, with the lane names in the message |
| `tests/tick/test_report.py` | The slow lane report steps and a `CallStep` keeping the lane it is given |
| `tests/tick/test_closing_lane.py` | The closing deadline starting once at `Pass.close` and outliving an expired critical lane, a closing step running while a critical step in the same group is dropped, the three closing steps in the `closing` group and lane, and a 300 second hung step under a 1 second critical lane still stamping last tick, the heartbeat and the status |
| `tests/tick/test_executors.py` | Three groups of eight fanning out steps using at most `workers` `tick-step` threads, `workers` `tick-fan-out` threads and one `tick-slow-lane` thread; both executors refusing work after the pass; a queued fan out cancelled at pass end; a nested fan out running inline; a group whose needs never end refused on the first idle round; a step waiting on a handed off step holding no worker, also after the critical lane ended; and `_waiting` true only for a pending handed off need inside a live lane |
| `tests/tick/test_early_end.py` | An early end names each left step once and counts the missing value without writing last tick or the heartbeat itself |
| `tests/tick/test_pool.py` | A parallel group returning its critical records while its slow step runs, a handed off step starting after the critical step it needs and dropped when that step was dropped, a handed off step applying its writes through the group appliers on a daemon thread named `tick-slow-lane`, the pass waiting for a hung slow step only until the slow deadline plus the grace second, and a sequential pass running slow steps in the group |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Undeclared step timing` at import | Declare that exact step name in `FLOORS` before constructing its `CallStep` |
| A step is `late` while its call remains active | The waiting budget expired; the worker keeps running and its result is discarded. Check the live report and the call before changing the measured declaration |
| Spawn takes longer than forty seconds to start | Check the site barriers of the [flat pass graph](#the-flat-pass-graph) first; the forty second assertion grades the prerequisite path, not the sequential pass wall time |
| Changing `interval_s` or `AGENTIHOOKS_TICK_CADENCE` does not reduce call frequency | Only intervals above 60 seconds rest a step, and only while the pass records to the swarm store. Read the stored ends with `timing.step_ends`; a step that ends `late` or `held` stores no end and stays due every pass. See [Interval enforcement](#interval-enforcement) |
| `Interval of <step> above 60s writes pass:<key>` at registration | The step writes the pass state, which every later step of the pass reads; declare it at `CRITICAL_S` |
| A wide or slow step runs on every pass | Its last run never ended `ok` or `failed`, the store or swarm is missing from the pass, or reading `tick:step-ran` fails. Read `timing.step_ends` for the swarm and the step's outcome rows in `tick-report` |
| `Unordered writers in lanes tick and slow` on the first group run | A slow step and a critical step touch the same domain with no dependency; add the step that must run first to `needs`; moving both into one lane does not clear it |
| `Unordered reader and writer ... for pass:<key>` on the first pass | A step reads a pass box key with no need path to the step that writes it; add that writer, or a step that needs it, to the reader's `needs` |
| `test_pass_edges` names `reads pass <key> without declaring it` | The step reads that key from the box or its pass view; add `pass_domain("<key>")` to its `reads` and its writer to its `needs` |
| A critical step is recorded late with no error | It needs a slow lane step, so it was handed off and waited for that step while the critical deadline ran out. Drop the need or keep the needed step critical |
| `swarm_tick_step_abandoned` in the tick journal | That step was still running at its budget or its lane deadline and the writes it names were dropped. Its worker may still finish its external calls. One line now and then is a slow call; the same step on most passes means its budget is below its real wall time, see [The abandoned write journal line](#the-abandoned-write-journal-line) |
| A step is late in `tick-report` but no `swarm_tick_step_abandoned` line exists | The step declares no write beyond its own `actions:<name>` domain, or it was dropped before it started because a step it needs was dropped or its lane had expired. Neither loses writes |
| `fleet red notice` is late in a pass with no critical lane overrun | It needs `spawn`, a step in a later group; check `spawn`'s own record in the same pass for a late or dropped result first |
| No heartbeat or last tick after a pass that printed actions | The pass raised or returned before its end, so `ended` was none and the closing group stamped nothing; read the tick log for the exception or the early return line. A pass that ended with `pass ended early` does stamp both |
| `closing` steps recorded late | The step's own budget or the 30 second closing deadline ran out. The deadline starts at the pass `finally`, not at the start of the pass, so a long critical lane does not shorten it |
| `Dependency cycle: <names>` from `run_group` | The named steps of one group are unplaced: they sit on or behind a `needs` cycle among the group's steps. Remove the need that closes the cycle |
| `wait on needs that never end` | `_eager` was called directly with steps that need each other and none waits on a handed off step of an earlier group; `run_group` refuses the same steps earlier with `Dependency cycle`. Fix the `needs` cycle |
| `RuntimeError: cannot schedule new futures after shutdown` from a step or fan out | The submit ran after its pass ended, for example from a worker that outlived the pass. The pass executors close at pass end; start such work inside a pass |
| `slow lane step <name> failed` in the tick journal | That handed off step raised; its error follows the colon. The critical work of the pass already finished |
| A test patch on `scripts.tick.steps.common._rebind` (or another helper) has no effect | The helpers call their siblings through `scripts.swarm.tick`; patch the name there |
| `AttributeError: module 'scripts.swarm.tick' has no attribute ...` from a common helper | A moved helper names a tick module attribute that no longer exists there; import it back into the tick module or reference its new home |
| A step never runs | `engine.group_steps("<group>")` lists the registered steps of a group; a missing step means its module is not listed in `scripts.tick.steps.MODULES` |
| Import of the tick fails with `Duplicate step name` | Two registered steps share a name. Rename the new step; every `CallStep` writes only its own `actions:<name>` domain, so distinct names also keep `waves()` from refusing unordered writers |
| Spawn never places an agent | Expected under the events engine: `readiness.tick_launches` is always false, so `_spawn_step` returns before it reaches `tick._spawn`. Hives take tasks over the event bus instead |
| Spawn runs no calls | The swarm is paused, on the events engine or in panic, or `readiness.tick_launches` refused; rank pass and bottleneck remedy still run |
| A `dropped write` line in the tick actions | Expected under a concurrent change: the guarded field moved or the ledger refused the call. The next pass recomputes it; a line that repeats every pass for the same task means something keeps changing that field, or the ledger keeps refusing the write |
| A size or group change never lands and no `dropped write` line appears | The site had no `ledger` or `slug` in its pass state, so `tasks.appliers` returned no appliers and the writes stayed on the record |
| `Step <name>: feature '<id>' is not a feature map id` from `catalogue check` | The step's `feature` names no `id` in `feature-map.json`; use an existing id or add the entry |
| `Step <name>: no scenario lists it` from `catalogue check` | The step has no row, or an empty list, under `scenarios` in `tests/swarm/tick_scenarios/steps.json`; add the scenario names it runs in |
| `Step <name> is mapped; drop it from the unmapped steps` | The step now has a feature map id and a scenario; remove its name from `unmapped` |
| `Unmapped step <name> is not registered; drop it` | The step was renamed or removed; remove the old name from `unmapped` |
| `tests/swarm/tick_scenarios/steps.json names unknown scenarios: <names>` | The index lists a scenario with no golden in `goldens.json`; use a golden key without its `run_tick/` or `tick/` prefix |
| `Unmapped step <name> is new; map it to a feature and a scenario instead` from `catalogue shrink` | The head added a name to `unmapped` that the base does not hold. Map the step instead; the base copy of the ratchet runs in CI, so editing the head's `catalogue.py` does not clear it |
| A tick pass raised and later steps of the same group did not run | Expected: the first moved step that raised holds the exception and the rest of the group's moved steps skip until it is raised at the site |
