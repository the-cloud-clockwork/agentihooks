---
title: Tick load budget
parent: Reference
nav_order: 65
---

# Tick load budget

`tests/swarm/test_tick_load.py` runs one full tick pass under simulated load on
a virtual clock and holds its duration to a pass budget. The budget lives in
`tests/swarm/tick_scenarios/load_budget.json`, starts at 384 seconds and may
only go down: a pull request that raises it fails CI. Together with the golden
characterisation in `tests/swarm/tick_scenarios/`, it guards every later change
to the tick: a change that makes one tick step 300 seconds slower turns the
suite red.

## What the simulated pass runs

`load()` builds one swarm `sw` and runs `scripts.swarm.cli.run_tick` once,
fenced by the controller lease like a live tick.

| Input | Value |
| --- | --- |
| Ledger tasks | 900 (`TASKS`), ids `t1` to `t900`. Every third task is in the `ci` lane, the rest in `eng`: 600 `eng`, 300 `ci` |
| Working agents | 15 (`AGENTS`): 10 engineers and 5 ci agents, each holding a claimed task, live in the runtime and started before the startup grace |
| Lane caps | `max_eng=10`, `max_ci=5`, so the pass spawns no worker |
| Ledger latency | 2 seconds per call to any public method of the fake ledger |
| Runtime latency | 1 second per call to any public method of the fake runtime |
| Redis latency | 10 milliseconds per Redis command |

The latencies are in `LATENCY_S`. The ledger and runtime are the tick test
fakes `FakeLedger` and `FakeRuntime` from `tests/swarm/test_tick.py`; herdr is
`FakeHerdr` from `tests/swarm/test_delivery.py`; Redis is a `fakeredis` server
wrapped by `SlowClient`, which charges each command before it runs.

Settings and stubs that keep the pass deterministic:

| Setting or stub | Value |
| --- | --- |
| `AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES` | `0` |
| `AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES` | `0` |
| Merge stall GitHub reads | The characterisation's canned GitHub reply |
| Hive id, hostname, boot id | `fixture-hive`, `fixture-host`, a fixed boot id |

The pass also spawns the master, since none is online; the load report counts
only the 15 workers.

## The virtual clock

`VirtualClock` holds seconds that pass only when a collaborator call is
charged to it. Each charge adds the surface's latency and counts the call under
its surface name: `ledger`, `runtime`, `redis`, `sleep` and, in the slowdown
test, `planted`. `VirtualTime` replaces the `time` module in
`scripts.swarm.timing` and the tick core, and patches `time.time`;
`monotonic`, `perf_counter`, `time` and `time_ns` all read the virtual clock
and `sleep` charges it. `cli.now_ms` reads it too. The tick's own computation
costs no virtual time, so the measured pass is the sum of its collaborator
latencies and repeats exactly on every runner.

Because `fakeredis` reads the patched `time.time`, the controller lease also
expires on virtual time. A tick renews its lease for three ticks, 180 seconds
by default (see [The minute tick](../pillars/swarm.md#the-minute-tick)).

## How the pass is measured

The pass is timed by the tick's own instrumentation, which writes the pass
record to the swarm's `tick:passes` stream and its steps to `tick:steps`. The
test reads them back with `scripts.swarm.tick_report.read`, the same function
behind `agentihooks swarm <id> tick-report`, and compares `pass_p95_s`, which
for one pass is that pass's wall time, with the budget.

Each run prints one line to the test output:

```text
TICK_LOAD={"agents": 15, "calls": {"ledger": ..., "redis": ..., "runtime": ...}, "outcome": [...], "pass_count": 1, "pass_p95_s": ..., "steps": [...], "tasks": 900, ...}
```

pytest captures it on a passing test; it appears in the CI log of a failing
test, or with `-s` in a CI dispatch. `calls` shows how many charged calls each
surface took, so a change in the pass time can be traced to the surface that
gained calls. `steps` carries the per step count, median, p95 and CPU share of
the tick report.

## The tests

All five tests carry `@pytest.mark.feature("swarm-tick-pass-budget")`.

| Test | Passes when |
| --- | --- |
| `test_simulated_load_pass_fits_the_budget` | The pass sees 15 workers, 900 tasks and one recorded pass, `run_tick` returns its action list, and `pass_p95_s` is at most the budget |
| `test_a_step_made_300_seconds_slower_breaks_the_budget` | A second pass with `delivery.migrate_outbox` charged 300 extra seconds, called exactly once, ends above the budget while the unchanged pass stays within it; the budget headroom over the unchanged pass is under 300 seconds; the slowed pass ends with `the controller lease is stale` |
| `test_the_budget_only_shrinks_against_its_base` | The budget in the file is at most the budget on `origin/dev` and at most 384 seconds |
| `test_a_raised_budget_is_refused` | `check_budget` refuses 385 over 384, 300 over 299.5, and 400 over a base of 500, since 400 is above the initial 384 |
| `test_an_equal_or_lower_budget_is_accepted` | `check_budget` accepts 384 against 384, 200 against 384 and 299.5 against 300 |

The planted 300 seconds outlast the 180 second lease, so the tick finds its
lease stale and stops before its later steps. The slowdown test therefore
asserts the stale lease and the headroom rather than the full 300 added
seconds: the headroom check keeps the budget close enough to the measured pass
that any 300 second slowdown crosses it.

## The budget file and the shrink rule

```json
{
  "pass_budget_s": 384
}
```

`check_budget(current, base)` refuses a budget above `min(base, 384)` with:

```text
pass budget <current>s rises above <ceiling>s; a pull request may only lower it
```

`base_budget()` reads the base from git. It requires `origin/dev` to resolve,
then reads `tests/swarm/tick_scenarios/load_budget.json` at `origin/dev` with
`git show`. When `origin/dev` holds no budget file, the base is the initial 384
seconds.

### Lowering the budget

1. Make the tick faster, then read the new `pass_p95_s` from the `TICK_LOAD`
   line of a CI run.
2. Set `pass_budget_s` to a value at or above that pass and below the current
   budget, in the same or a later pull request.
3. Keep the headroom under 300 seconds; a lower budget only shrinks it.

A budget below the measured pass fails
`test_simulated_load_pass_fits_the_budget`. Once the lower value merges into
`dev`, it is the new ceiling for every later pull request.

## Feature map entry and live check

`feature-map.json` carries the entry `swarm-tick-pass-budget`, origin
`inventory`, ledger item `ledger:rig-grade-swarm/tasks/tc-a4`, seam
`scripts.swarm.cli.run_tick timed by its pass record and read back through
tick_report.read`, and the five tests above. Its live check is:

```bash
python -m scripts.install swarm rig-grade-swarm tick-report
```

The Doctor runs it from the repository root on its timer (see
[Feature live checks](feature-live-checks.md)). It reports the live swarm's
recorded passes, pass p95, idle gaps and per step costs, and exits zero when
the report reads. It does not compare the live pass p95 with the budget: the
budget is enforced in CI against the simulated pass, while the live check
shows the same measurement on the running tick. A Redis or argument failure
exits non zero and raises a `feature regressed` finding.

Output format:

```text
Tick report: <passes> recorded passes
Pass p95: <seconds>s
Gap p95: <seconds>s; largest gap: <seconds>s
Passes without retained steps: <count>
Step  Count  Median  P95  CPU share
<step>  <count>  <seconds>s  <seconds>s  <percent>%
CPU share uses summed own and reaped child CPU of retained steps, excluding the whole pass.
```

## Limits

- Only collaborator calls cost virtual time. A change that adds pure Python
  work inside the tick does not move the simulated pass; a change that adds
  ledger, runtime or Redis calls does.
- The latencies are fixed per surface. A slow call on one method of the live
  ledger shows on the live tick report before it shows in this test.
- The pass runs one tick over working agents only: no worker spawns, no
  finished agents to retire and no task changes, so the cost of those paths
  is not part of the budget.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| `origin/dev does not resolve, so the base budget cannot be read` | The checkout has no `origin/dev` ref. Fetch it with `git fetch origin dev` before the run |
| `pass budget 400.0s rises above 384.0s; a pull request may only lower it` | The budget file was raised. Restore it to the `origin/dev` value or lower |
| `simulated pass <n>s exceeds <budget>s` | The change added charged calls. Compare `calls` and `steps` in the `TICK_LOAD` line of the failing run with a run on `dev`, and remove the extra ledger, runtime or Redis calls |
| `assert report["outcome"] == STALE` fails in the slowdown test | The lease window or the tick's lease check changed, so the slowed pass ran to the end. Read the slowed pass's `outcome` in its `TICK_LOAD` line |
| `assert budget() - baseline < SLOWDOWN_S` fails | The tick got faster than the budget by 300 seconds or more. Lower `pass_budget_s` towards the measured pass |
| The fit test fails with `agents` at 16 | A master record counted as a worker. The report counts agents whose lane is not the master lane |
