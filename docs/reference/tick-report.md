---
title: Tick report
parent: Reference
nav_order: 70
---

# Tick report

`agentihooks swarm <id> tick-report` summarizes the newest tick passes of one swarm from the records every pass
leaves in Redis. It is the measurement behind the step budgets, the pass budget hold and the rollout stage checks.

```bash
agentihooks swarm <id> tick-report               # the newest 100 passes
agentihooks swarm <id> tick-report --passes 60   # the window the step budgets use
```

`--passes` takes 1 to 50,000; any other value is refused before Redis is read. The pass budget hold reads its own
window of the newest 10 passes.

## Output

Example output; the figures are illustrative:

```text
Tick report: 60 recorded passes
Pass p95: 41.204s
Gap p95: 19.031s; largest gap: 22.880s
Passes without retained steps: 0
Step  Count  Median  P95  CPU share
reap  60  9.112s  14.511s  21.40%
...
Lane slow  550 steps  64.20% CPU share
Lane tick  2940 steps  35.80% CPU share
Critical lane end p95: 38.402s
Step  Records  Late share  Abandoned  Held  Skipped  Dropped
fleet red notice  60  8.33%  5  0  0  15
...
CPU share uses summed own and reaped child CPU of retained steps, excluding the whole pass.
```

| Line | Meaning |
|---|---|
| Recorded passes | How many pass records the window found; fewer than `--passes` when the stream holds fewer. |
| Pass p95 | The 95th percentile of pass wall time over the window. |
| Gap p95, largest gap | The idle time between the end of one pass and the start of the next. A gap well above the cadence means the scheduler fired late or a pass was refused. |
| Passes without retained steps | Passes in the window whose step records were already trimmed from `tick:steps`. |
| Step rows | One per step name, sorted by name: how many times it ran in the window, its median and p95 wall time, and its share of the CPU all retained steps used. Registered engine steps appear under their registered name, such as `spawn`; other rows name the function they time. The whole pass measurement, `run_tick`, is left out. |

Times print with three decimals and CPU share with two; `n/a` means the window held no value. CPU share sums each step's own thread CPU
and the CPU of child processes it reaped. Step measurements can nest, so their shares can overlap.

## Lane lines

After the step rows the report prints one line per lane, sorted by lane name:

```text
Lane <lane>  <steps> steps  <share>% CPU share
```

`<steps>` counts the step records of that lane in the window and `<share>` is their own and reaped child CPU as a
percent of the CPU of every retained step. Only registered engine steps record a lane, so the lane shares can add up
to less than 100 percent; a window with no lane records prints no lane line. `tick` is the critical lane, where every
step runs unless it declares `slow`; see [Lanes](tick-engine-steps.md#lanes) for which steps run in the slow lane.

The critical lane share decides whether each lane runs in a process of its own, killed at its deadline after
releasing its locks: that is built only when the critical lane line reads above 40 percent. The window is sixty
parallel passes after the post spawn report steps moved to the slow lane, as the pull request that moved them
records. Read it with:

```bash
agentihooks swarm <id> tick-report --passes 60 | grep '^Lane '
```

Without `--passes` the report reads 100 passes.

| Reading | Window | Source | Result |
|---|---|---|---|
| `Lane tick` 43.61 percent, no step in the slow lane | 50 passes on dev, before the move | stated in the pull request 9517 body, 3 November 2026; the command output and pass window were not retained | The post spawn report steps moved to the slow lane |
| `Lane tick` after the move | 60 parallel passes | not yet recorded | Decides whether lane processes are built |

Until the second reading is taken lane processes are not built: both lanes run as threads of one tick process (see
[Lanes](tick-engine-steps.md#lanes)).

## Outcome columns

After the lane lines the report prints the critical lane end and one outcome row per step name, sorted by name, from
the `tick:records` stream of the same passes:

| Column | Meaning |
|---|---|
| Critical lane end p95 | The 95th percentile over the window of each pass's latest step end outside the slow lane, in seconds from the pass start: start offset plus wall time. |
| Records | Engine step records of that step in the window, one per run, held or skipped run. |
| Late share | Percent of those records whose outcome is `late`: the step ran past its budget or was left running. |
| Abandoned | Runs still going at their budget or lane deadline; the engine left them running and dropped their result. |
| Held | Runs refused because the step's earlier run for the same swarm was still alive. |
| Skipped | Steps dropped before they started, because their lane deadline had passed or a step they need was dropped. |
| Dropped | Writes that never applied: compare and set conflicts of finished runs, plus every write domain an abandoned run declares. |

Only steps the tick engine runs write these records; a window without them prints the header alone and
`Critical lane end p95: n/a`.

## Where the numbers come from

| Stream | One record per | Fields the report reads | Kept |
|---|---|---|---|
| `tick:passes` | Tick pass | `pass_id`, `start_ms`, `end_ms`, `wall_s` | Newest 50,000 |
| `tick:steps` | Finished step | `pass_id`, `step`, `wall_s`, `own_cpu_s`, `reaped_child_cpu_s` | Newest 50,000 |
| `tick:records` | Engine step record | `pass_id`, `step`, `lane`, `outcome`, `wall_s`, `budget_s`, `start_s`, `abandoned`, `held`, `skipped`, `dropped`, `conflicts` | Newest 50,000 |

Both streams sit under the swarm's own Redis key prefix. A pass record also carries the active agent count, the
steps that failed and its outcome (`success`, `stopped` or `error`). The report takes the newest passes, then only the
step records whose `pass_id` belongs to one of them. A step record whose pass is outside the window is counted as an
orphan and left out of every row.

## What reads the same records

- **Step budgets.** Each budget in `scripts.tick.steps.budgets.MEASURED` is a step's p95 from a 60 pass report with
  fifty percent headroom; see [Tick engine steps](tick-engine-steps.md#measured-budgets-and-declared-intervals).
- **Pass budget hold.** Every pass computes the p95 of its last 10 pass records. Above the target of the stage
  `AGENTIHOOKS_TICK_STAGE` names (`S1` 384, `S2` 240, `S3` 120, `S4` 60 seconds, default `S1`), the tick holds new
  spawns and sends the master one notice; the hold clears when the p95 is back under the target. With fewer than
  10 records the previous decision stands.
- **Load budget.** CI runs a simulated pass and grades its `pass_p95_s` against a budget that only shrinks; see
  [Tick load budget](tick-load-budget.md).

`AGENTIHOOKS_TICK_CADENCE` does not change any figure in the report today: it sets the slow lane cadence, which no
pass reads yet, the scheduler cron stays every minute, and every pass still runs every step. See
[Cadence](../pillars/swarm.md#cadence) and [Swarm Tick](configuration.md#swarm-tick).

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| `Tick report: 0 recorded passes` | No pass has recorded for this swarm. Check the swarm id, that the scheduler ticks (`journalctl --user -t agentihooks-swarm`), and the journal for `swarm_tick_record_failed`. |
| Many passes without retained steps | Steps outnumber passes, so `tick:steps` trims first. Read a shorter window. |
| No `Lane` lines | The window holds no step record with a lane. Check that the swarm runs the current tick code. |
| `Lane tick` above 40 percent | The critical lane takes more step CPU than the 40 percent threshold. Find its heaviest rows and check whether each needs to finish before spawn; one that does not can move to the slow lane, see [Lanes](tick-engine-steps.md#lanes). |
| One step dominates p95 | Compare it with its budget in the timing table; a step past its budget is recorded late and the pass continues. See [Tick engine steps](tick-engine-steps.md#when-a-step-exceeds-its-budget). |
| Spawns stop and the master got a pass budget notice | The pass p95 is above the stage target. Read the report over 10 passes to find the slow steps; the hold clears by itself once the passes recover. |

Related: [The minute tick](../pillars/swarm.md#the-minute-tick), [Tick engine steps](tick-engine-steps.md),
[Tick adapters](tick-adapters.md), [Swarm elapsed thresholds](swarm-thresholds.md).
