---
title: Tick Redis round trips
parent: Reference
nav_order: 79
---

# Tick Redis round trips

The swarm keeps one shared Redis in Anton. A tick pass or a `swarm set` command
that runs on another machine pays a network round trip on every Redis call,
about eight milliseconds from the workstation after the Redis moved to Anton.
At that cost a pass that made thousands of calls ran for several minutes, and
at 02:17 UTC one `agentihooks swarm <id> set` took nine minutes
to return.

Three changes cut the tick's Redis calls, the Redis server time its scripts
hold and the payloads it writes, with the target that a tick pass and a set
command each finish in under a minute against the remote Redis. The database
and the Redis stay shared: no process copies, syncs or caches swarm state in a
second store. The table below covers the third change; the
[measurements](#measured-results) cover all three.

## What changed

| Area | Before | Now |
|------|--------|-----|
| Hive placement of one agent | Each lookup read every agent and every hive member record, about 65 ms per lookup, repeated per agent many times in a pass. | `scope.host` reads the agent's field in the `hosts` projection hash. Only when that field is absent, or `SWARM_HOST_PROJECTION_ENABLED` is off, does it fall back to `ExecutionRegistry.hive`, a fenced lookup of the agent's latest execution record that takes a few reads: the listed agents, the by-name head and that one execution record. On a swarm whose records are not yet migrated to per record keys, each record read loads the whole swarm state document; see [Fencing records](fencing-records.md). Inside a tick pass the joined member list is read once per Redis client. See [Hive placement reads](../swarm-v2/execution-identity.md#hive-placement-reads). |
| Agent execution read | The `_LATEST` Lua script decoded every execution record on each call. | In name mode it decodes only the records whose leading name is a current agent, and falls back to a full decode when that pattern does not match. Its output is unchanged. Pull request 8616 later moved executions onto the fenced state records read by `scripts.swarm.fencing`, and `_LATEST` no longer exists. |
| Published swarm status | Carried the whole ended lives list, 18.7 MB on the measured swarm, rewritten on every pass. | Carries the newest 100 ended lives (`HISTORY_SHOWN` in `scripts.swarm.status`). |
| Stored account list | `stored_accounts` scanned with `COUNT 1000`: 162 calls over a 161 thousand key keyspace. | `COUNT 10000`: 17 calls over the same keyspace. `fleet_held` already scanned with `COUNT 10000`. |

`SWARM_HOST_PROJECTION_ENABLED` defaults to on; its values, rollout and checks
are in [Hive placement reads](../swarm-v2/execution-identity.md#hive-placement-reads).

The page stream already dropped `history`, `transfers` and `launch_checks`
(`STREAM_UNREAD` in the ledger server) before this change, so the ledger page
never carried the ended lives list.

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
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-1h" --until now | grep swarm_tick_step | grep run_tick
```

A set command, timed end to end:

```bash
time agentihooks swarm <id> set max-eng-agents=8
```

### Measured results

Measured on rig-grade-swarm, with the tick on the workstation and the Redis in
Anton, using the journal and `time` commands above. The tick figures are the
median of the `run_tick` row.

| Measurement | Before | After |
|-------------|--------|-------|
| `swarm set max-eng-agents=8` | 3.9 s | 2.3 s |
| Tick pass, change one | 428.95 s | 122.96 s |
| Tick pass, change two | 122.96 s | 109.3 s (n=18) |
| Tick pass, change two, last window before change three ran | 109.3 s | 78.8 s (n=15) |
| Status report build, change three | 10.5 s | 4.8 to 6.9 s |
| Status payload, change three | 25.2 MB | 6.4 MB |
| `_LATEST` script server time, change three | 32.4 ms | 16.0 ms |

The set command meets the one minute target. The tick does not yet: its last
recorded median, 78.8 s, is above sixty seconds and predates change three
running on the workstation, which removes the hot spots measured in that pass.
No tick median after change three is recorded.

Redis serves no other client while a Lua script runs. At the time of this
change `_LATEST` showed as a 25 to 30 ms `SLOWLOG` entry on every call, with
32.4 ms measured server time, so halving it also shortens every other swarm
client's waits.

## Limits

- The cap applies to readers of the status. A reader that needs a life older
  than the newest 100 reads the `history` list.
- The ledger page never received ended lives, so the page is unchanged.
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
merged into dev as commit 6f5d43507, the third change for the task to finish the swarm
tick on the Anton Redis in under a minute. The CI runs it cites are no longer
retained by GitHub; the timings come from the source task's proof, measured with
the journal and `time` commands on this page.
