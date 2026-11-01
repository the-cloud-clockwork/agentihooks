---
title: Swarm elapsed thresholds
parent: Reference
nav_order: 68
permalink: /docs/reference/swarm-thresholds/
---

# Swarm elapsed thresholds

The swarm measures idle and waiting periods from the event that started them.
A slower controller pass no longer multiplies those periods by the number of
minutes between passes. Actions still run during a pass: a deadline becoming
due does not start another pass or interrupt one already running.

## Defaults and observation windows

These durations preserve the previous behavior at a one minute cadence. The
first observation counts as one, so a third observation spans two minutes and
a twentieth observation spans nineteen minutes.

| Decision | Start event | Duration or rule |
|---|---|---|
| Idle nudge threshold | First eligible idle observation | 2 minutes; equivalent to 3 observations at one minute intervals |
| Idle retirement | First eligible idle observation | 19 minutes; equivalent to 20 observations at one minute intervals |
| Idle with a claim health finding | First eligible idle observation | The existing `AGENTIHOOKS_HEALTH_IDLE_TICKS` threshold, default 3, reads elapsed one minute observations |
| Waiting on input | First observation of the current prompt | `input_ticks` derives from time spent on that prompt; existing consumers keep their thresholds |
| Failed retirement health finding | First failed retirement attempt | 2 minutes of continuing failed attempts |
| Dispatch seat bottleneck trigger | First report naming the current bottleneck | 2 minutes across reports naming the same bottleneck; the latest report must also be no more than 15 minutes old |
| Autoscale pending raise | First decision requesting the uninterrupted raise | 2 minutes before lifting the hold; subsequent increases remain limited to two seats per pass |
| Ledger slow probe | First slow pass | Still requires two consecutive slow passes; this change does not replace that streak with a timer |

The elapsed comparison allows 30 seconds of scheduling slack. A two minute
limit can therefore be reached after 90 seconds. At exact one minute and three
minute cadences, the following observations result:

| Time since first idle observation | One minute passes | Three minute passes |
|---|---|---|
| 0 minutes | First observation | First observation |
| 2 minutes | Idle nudge threshold reached | No pass |
| 3 minutes | Already past nudge threshold | Idle nudge threshold reached on this pass |
| 19 minutes | Idle retirement due | No pass |
| 21 minutes | Already past retirement threshold | Idle retirement due on this pass |

These are eligibility times while all other checks permit the action. The
[stale heartbeat nudge](../pillars/swarm.md#stale-nudge) has a separate ten
minute silence window; it is not a two minute reminder. An agent due for idle
retirement gets no stale nudge.

## Configuration and inspection

No new duration setting is required. Existing settings keep their names.
`AGENTIHOOKS_HEALTH_IDLE_TICKS` still accepts a count, but compares it with
elapsed one minute observations rather than the number of controller passes.
For example, 6 represents five minutes since the first idle observation,
subject to scheduling slack and the next pass:

```bash
AGENTIHOOKS_HEALTH_IDLE_TICKS=6 agentihooks swarm example-swarm status --json
```

This changes only that invocation. Put the variable in the controller's launch
environment to configure its scheduled health passes. It does not change the
fixed idle retirement or nudge durations.

For an existing `agentihooks controller run` installation, a three minute
controller interval uses its existing environment setting:

```bash
AGENTIHOOKS_CONTROLLER_TICK_SECONDS=180 agentihooks controller run
```

Use this setting on the controller only. The host-scheduled `swarm tick`
command refuses it because that scheduler runs every minute. Controller lease
duration also follows the controller interval; these elapsed observation
windows do not change that separate lease behavior.

Inspect the current findings and capacity decision in the same deployment
environment as the running controller:

```bash
agentihooks swarm example-swarm status --json
agentihooks swarm example-swarm autoscale --json
```

`autoscale` is read only: repeated inspection does not advance a stored raise.
A changed target preserves the start of an uninterrupted pending raise. An
interrupted raise clears it; the next raise starts a new hold. Capacity drops
still apply immediately. The two seat limit remains per pass, so slower passes
can still slow the climb after the initial hold.

## Holds and stored state

An active declared wait, operator activity, an outage or recovery, a ledger
hold, or owned automated cleanup can hold idle decisions. Held time does not
add to the stored idle observation count. The next eligible observation
continues from the count retained before the hold. The master is exempt from
idle retirement.

Waiting on the same input prompt retains its start time. A different prompt
starts a new interval; leaving the prompt clears that input wait.

Idle and input event starts are stored separately from agent rows in Redis,
with a one day TTL. Dispatch streaks and pending raises keep their start in
their existing records. Existing strict row readers remain compatible. Legacy idle counts,
dispatch streaks and pending raises without a start timestamp are anchored
using the previous one minute assumption. This preserves progress but cannot
reconstruct the historical cadence. Counts such as `idle_ticks` and
`input_ticks` can jump between passes because they describe elapsed time.

The ledger probe remains an exception: two slow passes three minutes apart
produce the alert at the second pass. Faster or slower sampling still affects
when those two observations exist. Prompt backlog counts changes in prompt
values, rather than controller passes, and is unchanged.

## Troubleshooting and evidence

| Symptom | Check and action |
|---|---|
| A decision arrives after its nominal duration | Compare the first observation with the actual pass times and check holds. No action runs between passes. |
| Idle counts increase by more than one | Expected with a slower cadence: counts represent one minute observations spanned by elapsed time. |
| A health setting on a status command does not change later findings | Configure the running controller's environment; a command's environment applies only to that invocation. |
| Autoscale still climbs slowly after the hold | Check the two seats per pass limit, quota, ready demand and host room in the reason. |
| Ledger slow alert does not appear after one slow sample | It requires a second consecutive slow pass. |

[Pull request 9274](https://github.com/the-cloud-clockwork/agentihooks/pull/9274)
records the implementation and cadence proofs. Its
[green focused run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/41331855281)
reports 13634 passing tests, and its
[mutation preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/41333972390)
passed. The elapsed cadence tests cover idle nudge and retirement at three
minute intervals, input waits, idle health and the ledger probe. These runs
establish regression behavior, not a production rollout.
