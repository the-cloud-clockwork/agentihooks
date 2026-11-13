---
title: Fleet red notice
parent: Reference
nav_order: 69
---

# Fleet red notice

When most open pull requests in a repository fail the same checks, those
checks say nothing about any one change. The swarm then holds an open task
whose pull request is red only from those shared checks: no engineer claims
it, and the ledger shows a notice on the task saying why. This page covers the
hold, the notice the tick posts each pass, the open pull request listing both
read, the ten minute listing cache that keeps the notice step off the tick's
critical path and that the tick's claim filter reads, and the notice step's
measured budget that lets the cache be saved.

Module: `scripts.swarm.fleet_red`.

## When a task is held

A task is held when all of these are true:

| Condition | Rule |
| --- | --- |
| The task is open | `state` is `open` and `pr_url` names a GitHub pull request |
| Its pull request is red | At least one check on it has a red result |
| Enough other pull requests are recent | At least three (`MIN_OTHERS`) other open pull requests in the same repository have a check that finished in the last six hours (`WINDOW_MS`) |
| Every red check is shared | Each red check on the task's pull request also fails on more than half of those recent other pull requests |

A pull request with one red check that the rest of the fleet does not share is
not held: that check points at the change itself.

## Where the hold applies

| Site | Function | Listing |
| --- | --- | --- |
| Tick claim filter | `fleet_red.checker(fleet_red.saved_reader(store, slug))`, built by `tick.claim_view`. Every ready task listing goes through it, `tick.ready_in_order` and `tick._claimable`, including `capacity.ready_work`, which the quota notice step reads | The saved listing below when one is saved; otherwise a live listing once per repository per call, not saved. A saved failed listing admits every task. A failed live listing admits every task and logs `fleet red hold skipped` |
| Swarm readiness | `fleet_red.checker()`, used by `scripts.swarm.readiness` for the hub readiness snapshot | Read live through the source control port once per repository per call; a failed listing admits every task and logs `fleet red hold skipped` |
| Notice step | The `fleet red notice` tick step: `fleet_red.holds` computes the new holds and the lifted ones, and the step returns them as writes. The engine applies each hold through the fleet red applier, which posts it with `fleet_red.post` only while the task is still open, unclaimed and on the same pull request, and drops it for the next pass otherwise | Read through the listing cache below |

The notice pass looks only at open, unclaimed tasks with a pull request. For
each newly held task it posts one ledger notice:

> The swarm holds this task unclaimed: its pull request URL is red only from
> CHECKS, which also fail on most other open pull requests. It is claimed
> again once those checks stop failing elsewhere.

It records the hold under the swarm key `fleet-red:<task id>` so the notice is
posted once. When the hold lifts, or the task is claimed, closed or loses its
pull request, the pass deletes that key; the task posts a new notice if it is
held again later. The tick action line reads
`task <id> held unclaimed, <url> is red only from <checks>`.

## The open pull request listing

Both sites list open pull requests through the repository's source control
port, `SourceControl.open_changes`, with a 60 second deadline (`SECONDS`).
`fleet_red.read(repo, ports, deadline=...)` takes an optional caller deadline
and passes `deadline.child(SECONDS)`, so the listing ends at the caller's
expiry or after 60 seconds, whichever comes first. Without a caller deadline
it passes a fresh `Deadline.after(SECONDS)`. The GitHub adapter checks the
deadline before its first GraphQL call and asks for open pull requests with
each head commit's check rollup:

| Setting | Value |
| --- | --- |
| Page size | 25 pull requests (`first: 25` in the adapter's open query) |
| Pages | At most 8 (`_OPEN_PAGES`), so at most 200 pull requests |
| Stop | The first page whose `hasNextPage` is false |

Pages of one hundred pull requests with their check rollups made GitHub answer
with a gateway timeout after about eleven seconds, so every tick pass spent
about thirteen seconds in the notice step and held nothing. Pages of
twenty five return within the deadline and keep the same two hundred pull
request ceiling.

## The listing cache

`fleet_red.cached(store, slug, repo)` keeps each repository's listing in the
swarm Redis for ten minutes (`TTL`, 600 seconds):

| Case | What is saved | What the next ten minutes return |
| --- | --- | --- |
| Listing succeeds | Each pull request URL with its checks' name, result and finish time, as JSON | The same pull requests, rebuilt as `OpenChange` and `RollupCheck` |
| Listing returns no pull requests | An empty list | An empty listing, so nothing is held |
| Listing fails with a port error (timeout, OS, key, value or subprocess error) | JSON `null` | No listing: the pass skips every task of that repository and posts no notice |

The key is per swarm and per repository:

```text
<key prefix>:swarm:<swarm slug>:fleet-red-pulls:<owner>/<repo>
```

The key prefix is `agentihooks` unless `AGENTIHOOKS_SWARM_KEY_PREFIX` sets
another. One tick pass therefore calls GitHub at most once per repository
every ten minutes, and a failing GitHub costs one failed call per ten minutes
instead of one per pass. A hold can start or lift up to ten minutes after the
checks change on GitHub.

The tick claim filter reads the same key through `fleet_red.saved_reader`:

| Saved under the key | The claim filter |
| --- | --- |
| A listing | Judges each task against it and does not call GitHub |
| JSON `null`, a failed listing | Gets no pull requests and admits every task, without calling GitHub |
| Nothing | Lists live once per repository per claim view and does not save the result; only the notice step saves |

Before this, the claim filter listed live on every claim view. A capacity
read spent 17.0 of its 22.0 seconds there, in four GitHub GraphQL calls,
which made the quota notice step the slowest step of the pass.

## The notice step budget

The engine waits on a step for its budget. A step still running when its
budget runs out is abandoned, and its result, the writes it returns included,
is dropped (see
[When a step exceeds its budget](tick-engine-steps.md#when-a-step-exceeds-its-budget)).
A step that finishes just past its budget is recorded late but keeps its
writes. The listing save is one of those writes. With no measured timing the
`fleet red notice` step had the fifteen second unmeasured budget, while one
live listing took about 16.6 seconds, so the step was abandoned on every pass
that listed, the save never landed and every pass listed again.

`budgets.FLOORS["fleet red notice"]` carries a 30 second floor from the step's measured p95 of
19.981 seconds, from `tick-report --passes 10` on 3 November 2026 at 18:30
UTC. Its budget is `ceil(19.981 * 1.5)`, 30 seconds, above the live listing,
so the pass that lists keeps its save and the passes of the next ten minutes
read it.

| Measure, ten passes | Before | After |
| --- | --- | --- |
| Fleet red notice median | 17.410 s | 0.991 s |
| Fleet red notice p95 | 20.265 s | 18.811 s, the pass that lists again |
| Quota notice median | 26.605 s | 8.236 s |
| Quota notice p95 | 32.908 s | 26.757 s |

The after sample is ten passes that all started after the change reached
the workstation's dev checkout at 20:14 UTC that day. The first of them ran
the notice step in 18.81 seconds and was not late, so its save was kept.

Read the declared budget:

```bash
python -c 'from scripts.tick.steps.budgets import timing_of; print(timing_of("fleet red notice"))'
```

It prints `(30.0, 60.0)`: a 30 second budget and a 60 second interval.

## The slow lane

The `fleet red notice` step is a `WriteStep` in the `reopen passes` group with
`lane=SLOW`. In a parallel pass the group hands it off: it starts once the
group's critical steps (dev red reopen, prerequisite reopen, promotion
refresh, release alert) end, and runs on the slow lane thread while the pass
goes on through `task passes` and into the placing lock. Its writes, the
listing save included, are applied through the group's appliers when it ends,
so a notice step that ends within its 30 second budget keeps its save on the
slow lane exactly as it did on the critical lane.

The order is the other way around now: `fleet red notice` needs `spawn`, so
it runs after placement, not before. No step needs `fleet red notice` in
turn, so nothing else waits on it or is dropped with it. When the notice
step runs past its budget it is abandoned on its own: its writes are
dropped and recorded late for that pass, and the tick journal gets one line
naming the lost writes:

```json
{"event": "swarm_tick_step_abandoned", "slug": "rig-grade-swarm", "step": "fleet red notice", "budget_s": 30.0, "writes": ["fleet red", "fleet red lifted", "fleet red listings"]}
```

Find these lines:

```bash
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-6h" --until now \
  | grep '"event": "swarm_tick_step_abandoned"' | grep '"step": "fleet red notice"'
```

Read the lane the step runs in:

```bash
python3 -c 'from scripts.tick.steps.tasks import STEPS; print([s.lane for s in STEPS if s.name == "fleet red notice"])'
```

It prints `['slow']`. See [Lanes](tick-engine-steps.md#lanes) for how the
pass hands off slow steps and
[The abandoned write journal line](tick-engine-steps.md#the-abandoned-write-journal-line)
for every field of the line.

## Usage

Read the step's time over the last sixty tick passes:

```bash
agentihooks swarm <swarm slug> tick-report --passes 60
```

Read or clear the cached listing of one repository, here for swarm
`rig-grade-swarm` and repository `the-cloud-clockwork/agentihooks`:

```bash
redis-cli TTL agentihooks:swarm:rig-grade-swarm:fleet-red-pulls:the-cloud-clockwork/agentihooks
redis-cli GET agentihooks:swarm:rig-grade-swarm:fleet-red-pulls:the-cloud-clockwork/agentihooks
redis-cli DEL agentihooks:swarm:rig-grade-swarm:fleet-red-pulls:the-cloud-clockwork/agentihooks
```

A deleted key makes the next tick pass list the repository again.

Pass a reader to call the notice pass or the cache without GitHub:

```python
from scripts.swarm import fleet_red

found, lifted = fleet_red.holds(slug, store, rows, now_ms, reader=lambda repo: pulls)
listing = fleet_red.cached(store, slug, "owner/repo", reader=lambda repo: pulls)
```

A `reader` passed to `holds` replaces the cache, so it lists on
every call.

Build the tick's claim filter over the saved listing, as `tick.claim_view`
does, and judge one task row:

```python
admit = fleet_red.checker(fleet_red.saved_reader(store, slug))
claimable = admit(task_row)
```

`admit` returns false for a task it holds. It keeps each repository's
listing for the life of the checker, so one claim view reads Redis, or
GitHub on a miss, once per repository.

## Configuration

| Name | Where | Default | Effect |
| --- | --- | --- | --- |
| `MIN_OTHERS` | `scripts.swarm.fleet_red` | 3 | Fewest recent other open pull requests before any hold |
| `WINDOW_MS` | `scripts.swarm.fleet_red` | 6 hours | How recent a check must be for its pull request to count |
| `SECONDS` | `scripts.swarm.fleet_red` | 60 | Deadline of one listing |
| `TTL` | `scripts.swarm.fleet_red` | 600 | Seconds a listing, or a failed one, stays cached |
| `_OPEN_PAGES` and `first:` | `scripts.tick.adapters.github` | 8 pages of 25 | Listing page size and ceiling |
| `AGENTIHOOKS_SWARM_KEY_PREFIX` | Environment | `agentihooks` | Root of every swarm Redis key, the cache key included |

The constants are code: change them through a pull request.

## Limits

- A repository with more than two hundred open pull requests is judged on the
  first two hundred GitHub returns.
- A hold, a lifted hold and its notice lag GitHub by up to ten minutes.
- A failed listing stays cached for ten minutes, so a short GitHub outage
  suppresses new notices for that long, and the tick claim filter admits
  every task for that long.
- Swarm readiness, the hub readiness snapshot, still lists live on every
  call.
- A claim view on a cache miss lists live without saving, so until the notice
  step saves, each claim view pays one listing per repository.
- The notice step budget is a checked in measurement. A live listing that
  grows past 30 seconds makes the step late again and drops its save.
- The notice step is on the slow lane and now needs `spawn` instead of gating
  it: nothing waits on the notice step, so an abandoned notice step only
  drops its own writes for that pass.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The notice step still costs seconds every pass | `tick-report --passes 60`; then `TTL` on the cache key. A missing key on every pass means the save fails; read the tick log for Redis errors |
| No notice although most pull requests share a red check | `GET` the cache key. `null` means the last listing failed: delete the key to list again, or wait out the ten minutes. Otherwise confirm at least three other pull requests have a check that finished in the last six hours |
| A task stays held after the shared checks went green | The cached listing is up to ten minutes old; delete the key to read GitHub now |
| A task is claimed although the notice pass holds it | `GET` the cache key: `null` makes the claim filter admit every task. With no key the claim filter lists live and admits every task when that listing fails; read the tick log for `fleet red hold skipped` |
| The cache key never appears and the notice step runs over its 30 second budget | `tick-report --passes 10` shows the step's wall time; a step still running at its budget is abandoned and its save dropped. Compare that wall time with `timing_of("fleet red notice")` and raise its floor in `budgets.FLOORS` through a pull request, or let its live budget follow its walls |
| `fleet red notice` is late in a pass with no critical lane overrun | It needs `spawn`, so it starts only once `spawn` ends; a late `spawn` record in the same pass explains the delay. Compare the step's wall time in `tick-report --passes 10` with its 30 second budget and raise its floor in `budgets.FLOORS` through a pull request, or let its live budget follow its walls |
| The quota notice step costs tens of seconds | Its capacity read builds the claim view; with no cache key every read lists GitHub. Check the cache key's `TTL` and the notice step as above |

## Tests

| Test file | Covers |
| --- | --- |
| `tests/swarm/test_fleet_red.py` | Hold rules, the notice pass, the cache listing once per ten minutes, one cache entry per swarm and repository, a failed listing cached as none for each port error, an empty listing cached, the default reader, the notice pass reading through the cache, no notice while a failed listing is cached, the claim view judging a saved listing without listing again, the claim view admitting every task under a saved failed listing, and the notice step budget above the 16.6 second live listing |
| `tests/tick/test_apply_fleet_red.py` | The notice step's guarded hold writes and lifted write, the applier posting while the task stays open and unclaimed, dropping a hold whose task moved or a seat claimed, forgetting a failed notice, and a claimed hold dropped then posted on the next pass |
| `tests/tick/test_tasks.py` | The notice step in lane `slow` with its measured budget, and a `WriteStep` keeping the lane it is given |
| `tests/tick/test_abandoned_writes.py` | A write step abandoned at its budget or at an expired lane journaling one `swarm_tick_step_abandoned` line with its sorted write domains |
| `tests/tick/test_source_control.py` | The GitHub open listing: one page when there is no next page, and the stop after eight pages of twenty five |

Related: [Tick engine steps](tick-engine-steps.md), [Tick adapters](tick-adapters.md),
[Tick load budget](tick-load-budget.md).
