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
read, and the ten minute listing cache that keeps the notice step off the
tick's critical path.

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
| Claim filter | `fleet_red.checker()`, used by the tick's claim view and by swarm readiness | Read live through the source control port once per repository per call; a failed listing admits every task and logs `fleet red hold skipped` |
| Notice pass | `fleet_red.notice_pass`, called once per tick pass after the task group and before the task passes group | Read through the listing cache below |

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
port, `SourceControl.open_changes`, with a 60 second deadline (`SECONDS`). The
GitHub adapter asks GraphQL for open pull requests with each head commit's
check rollup:

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
checks change on GitHub. The claim filter does not read the cache.

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

held = fleet_red.notice_pass(slug, store, ledger, rows, now_ms, reader=lambda repo: pulls)
listing = fleet_red.cached(store, slug, "owner/repo", reader=lambda repo: pulls)
```

A `reader` passed to `notice_pass` replaces the cache, so the pass lists on
every call.

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
  suppresses new notices for that long.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The notice step still costs seconds every pass | `tick-report --passes 60`; then `TTL` on the cache key. A missing key on every pass means the save fails; read the tick log for Redis errors |
| No notice although most pull requests share a red check | `GET` the cache key. `null` means the last listing failed: delete the key to list again, or wait out the ten minutes. Otherwise confirm at least three other pull requests have a check that finished in the last six hours |
| A task stays held after the shared checks went green | The cached listing is up to ten minutes old; delete the key to read GitHub now |
| A task is claimed although the notice pass holds it | The claim filter lists live and admits every task when its listing fails; read the tick log for `fleet red hold skipped` |

## Tests

| Test file | Covers |
| --- | --- |
| `tests/swarm/test_fleet_red.py` | Hold rules, the notice pass, the cache listing once per ten minutes, one cache entry per swarm and repository, a failed listing cached as none for each port error, an empty listing cached, the default reader, the notice pass reading through the cache, and no notice while a failed listing is cached |
| `tests/tick/test_source_control.py` | The GitHub open listing: one page when there is no next page, and the stop after eight pages of twenty five |

Related: [Tick engine steps](tick-engine-steps.md), [Tick adapters](tick-adapters.md),
[Tick load budget](tick-load-budget.md).
