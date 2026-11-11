---
title: Tick Redis round trips
parent: Reference
nav_order: 71
---

# Tick Redis round trips

The swarm keeps one shared Redis in Anton. A tick pass or a `swarm set` command
that runs on another machine pays a network round trip on every Redis call,
about eight milliseconds from the workstation after the Redis moved to Anton.
At that cost a pass that made thousands of calls ran for several minutes, and
one `agentihooks swarm <id> set` took nine minutes to return.

The tick and the set command now make fewer Redis calls and send smaller
payloads, so both finish in under a minute against the remote Redis. The
database and the Redis stay shared: no process copies, syncs or caches swarm
state in a second store.

## What changed

| Area | Before | Now |
|------|--------|-----|
| Hive placement of one agent | Each lookup read every agent and every hive member record, about 65 ms per lookup, repeated per agent many times in a pass. | One read of the agent's `hosts` projection field, or of its latest execution record when the projection is off or empty. The joined member list is read once per pass. See [Hive placement reads](../swarm-v2/execution-identity.md#hive-placement-reads). |
| Published swarm status | Carried the whole ended lives list, 18.7 MB on the measured swarm, rewritten on every pass. | Carries the newest 100 ended lives (`HISTORY_SHOWN` in `scripts.swarm.status`). The status payload fell from 25.2 MB to 6.4 MB. |
| Page stream | Sent the status as published. | Drops `history`, `transfers` and `launch_checks` (`STREAM_UNREAD` in the ledger server), which the page never reads. |
| Stored account scans | `SCAN` in default pages of ten keys: 162 calls over a 161 thousand key keyspace. | `SCAN` with `COUNT 10000`: 17 calls over the same keyspace. Used by `stored_accounts` and `fleet_held` in `scripts.swarm_v2.accounts`. |

## Ended lives in swarm status

The swarm keeps every ended life of an agent in its `history` list in Redis. A
row is appended when an agent's life ends, with the agent record, its
`execution_id`, `hive` and `runtime_backend`, `ended_at` and a `reason` of
`finished`, `retired` or `handed off`. The list itself is not trimmed by this
change.

Only the status view is capped:

```bash
agentihooks swarm <id> status --json | jq '.history | length'   # at most 100
agentihooks swarm <id> status --json | jq '.history[-1]'         # the newest ended life
```

`swarm status --json`, the published status the tick writes, and the swarm
view built from it return the last 100 rows, oldest first. Code that needs every
ended life reads the Redis list directly, as the store's restart and reclaim
lookups, the Doctor spawn reader, the swarm metrics and the master launch do.

To read the whole list, run the read with a client whose Redis user may read
the swarm keys:

```python
import json
from scripts.swarm.store import connect

store = connect()
rows = [json.loads(raw) for raw in store.redis.lrange(store.key("<id>", "history"), 0, -1)]
```

## Measuring the tick and a set command

Measure before and after a change the same way, against the same Redis.

Pass duration, from the pass records the tick report reads:

```bash
agentihooks swarm <id> tick-report --passes 60
```

The step rows show the median and p95 wall time of each step. See
[Tick report](tick-report.md) for each line.

Pass duration from the scheduler journal on the host that runs the tick:

```bash
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-1h" --until now | grep swarm_tick_step
```

A set command, timed end to end:

```bash
time agentihooks swarm <id> set max-eng-agents=8
```

Measured on rig-grade-swarm with the workstation tick and the Anton Redis:

| Measurement | Start | After the first two changes | Delivered change |
|-------------|-------|-----------------------------|------------------|
| `swarm set max-eng-agents=8` | 3.9 s | 2.3 s | under 60 s held |
| Tick `run_tick` median | 428.95 s | 109.3 s (n=18) | hot spots below removed |
| Status report build | 10.5 s | | 4.8 to 6.9 s |
| Status payload | 25.2 MB | | 6.4 MB |
| Execution read script server time | 32.4 ms | | 16.0 ms |
| `quiet_minutes` | 1.18 s | | 0.44 s |

The execution read script ran on every call as a 25 to 30 ms Redis `SLOWLOG`
entry, and Redis serves no other client while a script runs, so halving it
shortens every other swarm client's waits too.

## Limits

- The cap applies to readers of the status. A reader that needs a life older
  than the newest 100 reads the `history` list.
- The page never showed ended lives, so the page is unchanged.
- The round trip cost still scales with the number of calls. A new tick step
  that reads per agent or per task in a loop brings the cost back; batch the
  reads in a pipeline or one script. The
  [tick load budget](tick-load-budget.md) simulates the pass with Redis latency
  and fails a change that pushes the pass over its budget.

## Troubleshooting

| Symptom | Cause | Check |
|---------|-------|-------|
| `status --json` lacks an older ended life | The status view carries the newest 100 rows only. | Read the `history` list with `LRANGE` as shown above. |
| A tick pass runs for minutes again | A step reads Redis once per item. | `agentihooks swarm <id> tick-report --passes 60` names the slow step; read its code for per item calls. |
| A set command is slow | Its writes or the status refresh wait on Redis. | Time it with `time`, then read Redis `SLOWLOG GET` for long scripts that block other clients. |
| Placement lookups are slow | The `hosts` projection read is off. | See [Hive placement reads](../swarm-v2/execution-identity.md#hive-placement-reads). |

## Delivery evidence

Delivered by pull request 5491, "Cut swarm tick Redis server time and payloads",
for the task to finish the swarm tick on the Anton Redis in under a minute.
Red runs per seam: 39564777096, 39565304405, 39565648988, 39565917005. Green
proofs run 39571102885 (2617 passed).
