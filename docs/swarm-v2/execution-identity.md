# Execution identity metadata, version 2.0

SV2-IDN-02 extends AgentRecord with execution_id, generation, runtime_backend and runtime_target. Canonical names continue to come from NameRegistry. A seat, its memories and its task lineage survive changes of execution identity or canonical process name. These records provide identity metadata only; they carry no grant and are not evidence of worker liveness or permission to mutate a task.

RedisStore.start_execution admits an incarnation before runtime creation. Its input is a named AgentRecord without an execution ID or generation. The caller supplies the exact previous_execution_id for a replacement, or an empty predecessor for the seat's first tracked attempt. The store allocates a UUID and increments the seat's execution generation in one watched transaction. Generations describe executions of a seat; task ownership generations and controller epochs belong to the later fencing package.

A Kubernetes target contains pod_namespace, pod_name and an optional pod_uid. The UID is bound once through put_agent after runtime creation or reconciliation; all other target fields are immutable. Local targets may carry server_id, process_namespace and a positive pid. A missing local target preserves compatibility for metadata admission before the local runtime has started. Different Pods may share a display name; execution lookup accepts only the immutable execution ID. Reusing any previously recorded Pod UID is refused. UUID collisions, changed incarnation fields and stale predecessor or update requests are also refused. execution_identity_conflicts_total counts refusals per swarm without granting authority.

RedisStore.execution retrieves an exact attempt. RedisStore.executions returns the seat's ordered history. RedisStore.execution_occupants reconstructs the latest recorded incarnation per seat from persisted attempts, even when the current agents projection has been lost. This projection includes the last observation, including terminal states; the controller must consult runtime observations and execution authority before taking actions. Attempt records survive drop_agent, controller reconnection and the existing export/restore snapshot path. Latest attempt metadata can change through put_agent; superseded attempt records cannot be updated by that interface.

The preexisting local writer remains available for AgentRecord rows with default metadata. A remote record or a name already tracked in execution history must use admitted identity. Runtime adapter wiring and authenticated grants are separate dependent work packages; this package creates no Pods, processes or task leases.

Rollback: stop distributed launches before reverting the writer. Keep executions and seat memory as authoritative data. Old local readers can project the preceding AgentRecord fields; current readers also accept records that lack the added fields. The isolated rollback rehearsal checks this projection and verifies export/restore preserves all three attempts. Revert code through Git; do not delete attempt records to simulate rollback.

The agents hash remains a projection with the preceding record shape. New readers obtain additive identity from the executions hash in a consistent read transaction. This preserves the preceding strict dataclass decoder without requiring an old binary to understand the four added fields. Local writes watch both projection and execution history, so concurrent admission cannot be overwritten by an unadmitted legacy write.

## Parallel launches in one process

The swarm tick launches the seats of a pass in batches sized by the runtime's launch room, one thread per launch in a batch. Each launch admits its execution through `RedisStore.start_execution`, then binds its pane and updates its record. Each of these writes watches the swarm's `executions` hash and retries when another writer changes it first. A write gives up after five watched attempts (`WRITE_ATTEMPTS`).

Without the lock, the threads of one batch raced each other on the same hash. In a batch of several launches, a launch could lose all five attempts to its siblings. Its spawn then failed, the tick reopened the task when it still held it, and the Doctor reported a `failed-spawn/<task>` finding.

`ExecutionRegistry` holds one lock per swarm for these four writes:

| Method | Entry point | Error when five attempts are lost |
|---|---|---|
| `start` | `RedisStore.start_execution` | `execution history kept changing; admission was not committed` |
| `update` | `RedisStore.put_agent` for an admitted record | `execution history kept changing; update was not committed` |
| `set_pane` | `ExecutionRegistry.set_pane` | `execution history kept changing; pane was not committed` |
| `put` | `RedisStore.put_agent` | `agent registry kept changing; update was not committed` |

- Writes for the same swarm in one process run one at a time. A sibling launch waits for an admission in flight instead of spending its attempts against it.
- Writes for different swarms do not wait for each other. The lock is keyed by the swarm slug.
- The lock is reentrant. `put` on an admitted record calls `update` while it holds the lock.
- The lock lives in process memory. A writer in another process, such as a second tick or an `agentihooks swarm` command, still meets the watched transaction and its five attempts. The error can still appear when another process writes the same swarm's history that often.

Nothing is configured: the lock applies to every swarm and every launch path that writes through these four methods. `ExecutionRegistry.archive` and `ExecutionRegistry.end_orphan` also write the history without the lock; the tick runs them one at a time, outside the parallel launch batch.

### Reading a failed spawn

A launch that still runs out of attempts appears in the tick output as:

```text
<slug>: spawn failed for <task id>, task <task id> reopened: execution history kept changing; admission was not committed
```

The `, task <task id> reopened` part appears only when the tick still held the task and reopened it.

When the scheduled tick runs under `systemd-cat -t agentihooks-swarm`, its output is in the user journal:

```bash
# Spawn failures from execution history contention in the last day
journalctl --user -t agentihooks-swarm --since "1 day ago" \
  | grep "spawn failed for" | grep "execution history kept changing"
```

A line like this means a writer that does not take the lock changed the history during the launch: another process, or the archive and orphan cleanup writes. Look for such a writer before the failure time; the tick's own parallel launches no longer cause it. A reopened task launches again on a later pass. Never edit the `executions` hash by hand to clear a failure.

The tests in `tests/swarm/test_execution_concurrency.py` cover this behavior. They check that a sibling admission, update and pane write wait for an admission in flight, that another swarm writes during it, and that each of the four writes holds the lock of its own swarm.

## Current execution reads

Each swarm's fencing document holds its current executions in `executions` and its superseded attempts in
`archive`. `ExecutionRegistry.archive` moves a superseded attempt there once its name has left the swarm's agent list;
only a pane update for that exact attempt moves it back. The archive grows with the swarm's history: the description of
[pull request 9002](https://github.com/the-cloud-clockwork/agentihooks/pull/9002) reports nearly twenty megabytes of archived executions in the watched swarm's document. Hive
routing and roster reads used to load the whole document, archive included, on every call. A controller that is not a
hive member makes such a call for each agent it checks while hives are joined, so tick passes ran until they were
stopped at their 450 second deadline and sent no alert, nudge, merge or spawn.

Since [pull request 9002](https://github.com/the-cloud-clockwork/agentihooks/pull/9002), every fenced write also
publishes a copy of the swarm's `executions` alone, and these reads take that copy:

| Read | Used by | Reads |
|---|---|---|
| `ExecutionRegistry.hive` | `scripts.hive.scope.host`, and through it `scope.readings` for every agent the controller checks | current executions |
| `ExecutionRegistry.agents` | `RedisStore.agents`, the agent roster | current executions |
| `ExecutionRegistry.occupants` | `RedisStore.execution_occupants` | current executions |
| `ExecutionRegistry.managed` | cleanup | the `by-name` lookup, then the archive when the name has no current execution |
| `ExecutionRegistry.records`, `archive` and the fenced writes | seat history, cleanup, admission | the whole document, archive included |

Since [pull request 9279](https://github.com/the-cloud-clockwork/agentihooks/pull/9279) these reads go through the
records port, `fencing.records(store, slug)`, described in [Fencing records](../reference/fencing-records.md#the-records-port).
On a swarm still on the document layout the port takes the published copy for any read limited to `executions` and
`by-name`, and the whole document otherwise; the document stays authoritative and the copy is only read. A migrated
swarm keeps its authority in the per record keys. See
[Execution reads through the records port](#execution-reads-through-the-records-port) for each read on both layouts.

### How the copy stays consistent

The fencing state lives in one Redis state bucket shared by every swarm, under `agentihooks:swarm:state:fencing`
(`AGENTIHOOKS_SWARM_KEY_PREFIX` replaces `agentihooks`):

| Key | Type | Holds |
|---|---|---|
| `…:fencing:current` | hash, one field per swarm slug | the whole document and its revision |
| `…:fencing:revision` | counter | the bucket revision, raised by every write of any swarm |
| `…:fencing:history:<slug>` | list | the swarm's last 2 document revisions |
| `…:fencing:projected` | hash, one field per swarm slug | the swarm's `executions` alone, with the revision of the write that made it |
| `…:fencing:published` | string | the bucket revision at the last write that published a copy |

- **Write.** `RedisStateStore._write` publishes the copy and sets `published` in the same watched transaction as the
  document, so the copy never names a revision the document does not hold.
- **Read.** `fencing.read_executions` reads `revision`, `published` and the swarm's copy in one pipeline. It returns
  the copy only when `revision` equals `published`. Otherwise it falls back to `fencing.read` and takes
  `executions` from the whole document.
- **Gaps.** Two things leave `revision` ahead of `published`: a writer running code from before pull request 9002,
  which raises `revision` without touching `published`, and a write that lost its watch to a concurrent write of any
  swarm, because the writer raises `revision` before its transaction and retries. Every swarm's copy then stops being
  used at once and reads fall back. The next write that commits sees the gap, deletes the whole `projected` hash and
  publishes only its own swarm's copy. Every other swarm reads through the fallback until its own next write
  publishes a fresh copy. A stale copy is never returned; a gap costs only the whole document read.
- **Writers with no copy.** A `RedisStateStore` opened without a projection, such as the hive credentials and hive
  registry stores, touches `projected` and `published` only when its bucket already has a `published` key. There it
  deletes its own key's copy, or every copy when the bucket already had a gap, and sets `published`. For the hive
  credentials and hive registry stores, `python -m scripts.swarm.redis_commands` lists both keys and the `GET`, `SET`,
  `DEL` and `HDEL` commands; it does not map the fencing bucket. See the
  [Redis command map](../reference/redis-command-map.md).

A swarm moved onto per record keys no longer writes this document or its copy; its fencing state and the reads that
follow it are described in [Fencing records](../reference/fencing-records.md).

Nothing is configured. The copy is used only with the Redis state backend (`AGENTIHOOKS_STATE_BACKEND` unset or
`redis`); with the `bus` backend these reads load the whole document as before.

### Checking the copy

Compare `revision` with `published`, and the size of a swarm's copy with its whole document (read only):

```bash
K="${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}:swarm:state:fencing"
SLUG=rig-grade-swarm
URL="$AGENTIHOOKS_SWARM_REDIS_URL"
redis-cli -u "$URL" GET "$K:revision"
redis-cli -u "$URL" GET "$K:published"
redis-cli -u "$URL" HSTRLEN "$K:projected" "$SLUG"
redis-cli -u "$URL" HSTRLEN "$K:current" "$SLUG"
```

On a hive outside local mode, the swarm talks to `AGENTIHOOKS_HIVE_REDIS_URL` when it is set; use that URL there.
Equal `revision` and `published` mean every swarm with a copy reads it. A missing copy (`HSTRLEN` of `0`) or a lower
`published` means reads of that swarm fall back to the whole document until its next fenced write. Never write these
keys by hand; a fenced write such as the tick's next pass republishes the copy.

### Measuring tick passes

`scope.readings` and the whole pass (`run_tick`) both write `swarm_tick_step` records to the user journal. Total the
routing reads over a window, and count passes with outcome `stopped`, which covers a stop at the deadline and a stop by
the service:

```bash
journalctl --user -t agentihooks-swarm --since "-20min" \
  | grep '"event": "swarm_tick_step"' | grep '"phase": "finished"' \
  | grep '"step": "scripts.hive.scope.readings"' | sed 's/^[^{]*//' \
  | python3 -c 'import sys, json
w = sorted(json.loads(l)["wall_s"] for l in sys.stdin)
print(len(w), "calls; total", round(sum(w)), "s; median", round(w[len(w) // 2], 1) if w else None)'

journalctl --user -t agentihooks-swarm --since "-1h" \
  | grep '"event": "swarm_tick_step"' | grep '"phase": "finished"' \
  | grep '"step": "run_tick"' | sed 's/^[^{]*//' \
  | python3 -c 'import sys, json
rows = [json.loads(l) for l in sys.stdin]
print(len(rows), "passes;", sum(r["outcome"] == "stopped" for r in rows), "stopped")'
```

Add `grep "\"slug\": \"$SLUG\""` before `sed` to read one swarm. The Doctor measured these on the workstation
journal on 30 October 2026, after
[pull request 8616](https://github.com/the-cloud-clockwork/agentihooks/pull/8616) and before pull request 9002. The
routing reads of one twenty minute window took 403 seconds over 272 calls, against 11 seconds over 1746 calls in the
24 minutes before 8616 reached the checkout, and both Doctor passes of the following hour stopped at the deadline. A
live read only profile of three of these lookups, kept in the source task's proof on the Doctor ledger, took 8.9
seconds, 6.5 of them loading the whole document.

### Tests

`tests/events/test_state_projection.py` checks that the copy and the document commit at the same revision, that a
refused write leaves the copy unchanged, and that an older writer stops every copy being used until a new write
publishes its own. `tests/swarm/test_fencing_reads.py` checks that routing, roster and occupant reads transfer under
10,000 bytes per Redis reply with one or two hundred archived executions, that the archive is still read whole by
`fencing.read`, and that a document rewritten without a copy is read through the fallback with its new hive.

## Execution reads through the records port

Since [pull request 9279](https://github.com/the-cloud-clockwork/agentihooks/pull/9279), `ExecutionRegistry` reads
current and archived executions through `fencing.records(store, slug)` instead of loading the fencing document. The
port returns the per record Redis adapter for a swarm migrated with `python -m scripts.swarm.records_migration`, and
the whole document adapter, `fencing.WholeDocumentRecords`, for every other swarm. See
[Fencing records](../reference/fencing-records.md) for both layouts.

| Read | Records layout | Document layout |
|---|---|---|
| `ExecutionRegistry.hive(slug, name)` | One `by-name` field for the execution id, then one `executions` field | The published copy twice, once per lookup |
| `ExecutionRegistry.get(slug, execution_id)` | One `executions` field, then one `archive` field when the first is absent | The published copy, then the whole document with the archive when the id is not current |
| `ExecutionRegistry.managed(slug, name)` | One `by-name` field; the `archive` section only when the name has no current execution | The published copy, then the whole document with the archive |
| `ExecutionRegistry.occupants(slug)` | The `executions` hash | The published copy |
| `ExecutionRegistry.records(slug)` | The `executions` and `archive` hashes | The whole document with the archive |
| `fencing.read_executions(store, slug)`, used by `ExecutionRegistry.agents` | The `executions` hash | The published copy |

- `ExecutionRegistry.hive` still returns `""` for a name not in the swarm's agent list, and reads the `hive` of the
  execution the `by-name` index names, the one with the highest generation.
- `WholeDocumentRecords.sections` takes the published copy only when every requested section is `executions` or
  `by-name`, and only with the Redis state backend. When the copy is missing or behind the bucket revision, it reads
  the whole document as described in [How the copy stays consistent](#how-the-copy-stays-consistent). The `by-name`
  section is computed from the executions it read.
- `ExecutionRegistry.get` still refuses an unknown id with `unknown execution identity; display labels cannot identify
  attempts` and counts it in `execution_identity_conflicts_total`.

On a migrated swarm every fenced `ExecutionRegistry` write (`start`, `update`, `set_pane`, `archive` and
`end_orphan`) commits one batch of per record writes through `fencing.write`, each expecting the revision it read. A
conflict retries as in [Retries after a fencing conflict](#retries-after-a-fencing-conflict).

Nothing is configured for these reads and writes.

## Hive placement reads

`scripts.hive.scope.host(redis, slug, name)` names the joined hive member that runs an agent, so `scope.readings`
can find that agent's session readings below the member's key. The controller calls it for every agent it checks
while hives are joined.

### The host projection

Every agent projection write also writes the agent's hive into a separate hash, one field per agent name:

| Key | Type | Holds |
|---|---|---|
| `agentihooks:swarm:<slug>:hosts` | hash | Agent name to the hive member id from its record, `""` for an agent not on a hive |

`AGENTIHOOKS_SWARM_KEY_PREFIX` replaces `agentihooks`. The writes share the transaction that writes the
`agents` projection hash:

- `RedisStore.project_agent` sets the agent's field. A rename deletes only the old name's field and sets the new one.
- `RedisStore.drop_agent` and the retirement path that ends an agent delete its field with its `agents` field.
- The hive member Redis user may write the key: `swarm:*:hosts` is in `scripts.hive.auth.WORKER_KEYS`.
- Swarm snapshots export and restore the key with the other per swarm hashes.

### Turning the one field read on

`SWARM_HOST_PROJECTION_ENABLED` chooses the read:

| Value | `scope.host` reads |
|---|---|
| unset, or anything other than `true` (default) | `ExecutionRegistry.hive`, as in [the table above](#execution-reads-through-the-records-port) |
| `true` | One `HGET` of the agent's `hosts` field; `ExecutionRegistry.hive` only when the field is absent |

The joined member list comes from the hive index and member records. Inside `scope.one_pass()`, as in the
controller's pass, they are read once per pass, so each `scope.host` call with the flag on costs one `HGET`. Outside a
pass every call reads them again.

In both cases a member that is not among the joined members answers `""`, and a hive member process recognises only
itself.

The flag is off by default because a writer on older code updates execution records and the `agents` hash without
touching `hosts`, which leaves a field naming a hive the agent has left. Enable it only after:

1. Every controller, tick and hive member process runs a release that carries pull request 9279.
2. Every existing hive member Redis user is granted again, so its access list carries `swarm:*:hosts`. A user granted
   before the change cannot write the key, and its projection writes fail.

Set the flag through the deployment's git configuration, never by hand on a running pod or service. To go back, unset
it; the execution registry read needs no state from the projection.

### Checking a host field

Read only:

```bash
P="${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}:swarm"
SLUG=rig-grade-swarm
URL="$AGENTIHOOKS_SWARM_REDIS_URL"
redis-cli -u "$URL" HLEN "$P:$SLUG:hosts"
redis-cli -u "$URL" HLEN "$P:$SLUG:agents"
redis-cli -u "$URL" HGET "$P:$SLUG:hosts" "<agent name>"
```

The two lengths match when every projected agent has a host field. A field missing for a live agent is filled by that
agent's next projection write; with the flag on, the lookup falls back to the execution registry until then. Never
write the key by hand.

### Measuring the placement reads

The routing read is the `scripts.hive.scope.readings` step of the tick report; the journal command in
[Measuring tick passes](#measuring-tick-passes) prints its call count, total and median wall time. The source task's
target is a median under five milliseconds on the workstation, against twenty milliseconds over 29,508 calls before
the change. Pull request 9279 does not record that observation; it needs the deployed workstation with the flag on.

## Retries after a fencing conflict

A fenced write that loses to a concurrent write reads again and reruns its change, up to 16 attempts
(`fencing.ATTEMPTS`):

- On the records layout, `RevisionConflict` from the fenced batch.
- On the document layout, `StateConflict` from the state bucket update.

Before each remaining attempt the writer sleeps a random interval between 1 and 32 milliseconds
(`uniform(0.001, 0.032)` from `random`), so writers that collided do not retry in step. No sleep follows the last attempt.
After 16 conflicts the write raises `SwarmError("the swarm fencing state kept changing; nothing was committed")`, as
before. A stale lease raises at once with no sleep. The wait adds at most about half a second to a write that uses
every attempt. Nothing is configured.

### Tests for these changes

| Test | Checks |
|---|---|
| `tests/hive/test_member_scope.py::test_host_hash_hit_uses_one_field_read` | With the flag on, `scope.host` makes one `HGET` of the `hosts` field and never calls `ExecutionRegistry.hive` |
| `tests/hive/test_member_scope.py::test_host_hash_miss_keeps_legacy_lookup` | A missing `hosts` hash falls back to the execution registry |
| `tests/hive/test_member_scope.py::test_removing_an_agent_removes_its_host` | `drop_agent` deletes the field and the lookup answers `""` |
| `tests/hive/test_member_scope.py::test_host_projection_stays_untrusted_until_every_writer_upgrades` | With the flag off, a stale field left by an older writer does not decide the answer |
| `tests/hive/test_member_scope.py::test_renaming_an_agent_removes_only_its_old_host_projection` | A rename removes the old field and leaves other agents' fields |
| `tests/swarm/test_execution_concurrency.py::test_fencing_retries_sleep_only_before_remaining_attempts` | A conflict sleeps only while attempts remain, within the 1 to 32 millisecond bounds |
| `tests/swarm/test_fencing_reads.py::test_execution_and_name_sections_share_the_lightweight_projection` | Reads of `executions` and `by-name` take the published copy, not the whole document |

`tests/swarm/conftest.py` checks the bounds of every jittered sleep in the swarm tests.

### Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| A hive member's projection writes fail with a Redis permission error naming `…:hosts` | Its Redis user was granted before pull request 9279 | Grant the member again so its access list carries `swarm:*:hosts` |
| With the flag on, a controller reads an agent's session readings below the wrong member | A process on older code moved the agent without updating `hosts` | Unset `SWARM_HOST_PROJECTION_ENABLED` through git configuration, upgrade that process, then enable it again |
| `scope.readings` median stays near its old value with the flag on | The `hosts` field is missing for most agents, so lookups fall back | Compare `HLEN` of `hosts` and `agents` as above |
| `the swarm fencing state kept changing; nothing was committed` | Sixteen conflicting attempts, as in [Fencing records](../reference/fencing-records.md#troubleshooting) | The next pass retries; find the scanning change in the tick journal |
