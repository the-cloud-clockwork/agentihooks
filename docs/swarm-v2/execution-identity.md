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

Each swarm's fencing document holds its current executions in `executions` and its ended ones in `archive`.
`ExecutionRegistry.archive` moves a superseded attempt there once its name has left the swarm's agent list, so the
archive only grows. On the watched swarm it reached nearly twenty megabytes. Hive routing and roster reads used to
load the whole document, archive included, on every call. Every tick pass makes such a call for each agent it checks,
so a pass ran until it was stopped at its 450 second deadline and sent no alert, nudge, merge or spawn.

Since [pull request 9002](https://github.com/the-cloud-clockwork/agentihooks/pull/9002), every fenced write also
publishes a copy of the swarm's `executions` alone, and these reads take that copy:

| Read | Used by | Reads |
|---|---|---|
| `ExecutionRegistry.hive` | `scripts.hive.scope.host`, and through it `scope.readings` for every agent the tick checks | current executions |
| `ExecutionRegistry.agents` | `RedisStore.agents`, the agent roster | current executions |
| `ExecutionRegistry.occupants` | `RedisStore.execution_occupants` | current executions |
| `ExecutionRegistry.records`, `managed`, `archive` and the fenced writes | seat history, cleanup, admission | the whole document, archive included |

Full history and fenced write authority are unchanged: the document stays authoritative, the copy is only read.

### How the copy stays consistent

The fencing state lives in one Redis state bucket shared by every swarm, under `agentihooks:swarm:state:fencing`
(`AGENTIHOOKS_SWARM_KEY_PREFIX` replaces `agentihooks`):

| Key | Type | Holds |
|---|---|---|
| `…:fencing:current` | hash, one field per swarm slug | the whole document and its revision |
| `…:fencing:revision` | counter | the bucket revision, raised by every write of any swarm |
| `…:fencing:history:<slug>` | list | the swarm's last 64 document revisions |
| `…:fencing:projected` | hash, one field per swarm slug | the swarm's `executions` alone, with the revision of the write that made it |
| `…:fencing:published` | string | the bucket revision at the last write that published a copy |

- **Write.** `RedisStateStore._write` publishes the copy and sets `published` in the same watched transaction as the
  document, so the copy never names a revision the document does not hold.
- **Read.** `fencing.read_executions` reads `revision`, `published` and the swarm's copy in one pipeline. It returns
  the copy only when `revision` equals `published`. Otherwise it falls back to `fencing.read` and takes
  `executions` from the whole document.
- **Older writers.** A writer running code from before pull request 9002 raises `revision` without touching
  `published`, so every swarm's copy stops being used at once and reads fall back. The next new write sees
  the gap, deletes the whole `projected` hash and publishes only its own swarm's copy. Every other swarm reads through
  the fallback until its own next write publishes a fresh copy. A stale copy is never returned.
- **Writers with no copy.** A `RedisStateStore` opened without a projection, such as the hive credentials and hive
  registry stores, touches `projected` and `published` only when its bucket already has a `published` key. There it
  deletes its own key's copy and sets `published`, so only that key falls back. The Redis command inventory lists both
  keys and the `GET`, `SET`, `DEL` and `HDEL` commands for every such store.

Nothing is configured. The copy is used only with the Redis state backend (`AGENTIHOOKS_STATE_BACKEND` unset or
`redis`); with the `bus` backend these reads load the whole document as before.

### Checking the copy

Compare `revision` with `published`, and the size of a swarm's copy with its whole document (read only):

```bash
K=agentihooks:swarm:state:fencing
SLUG=rig-grade-swarm
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET "$K:revision"
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET "$K:published"
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HSTRLEN "$K:projected" "$SLUG"
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HSTRLEN "$K:current" "$SLUG"
```

Equal `revision` and `published` mean every swarm with a copy reads it. A missing copy (`HSTRLEN` of `0`) or a lower
`published` means reads of that swarm fall back to the whole document until its next fenced write. Never write these
keys by hand; a fenced write such as the tick's next pass republishes the copy.

### Measuring tick passes

`scope.readings` and the whole pass (`run_tick`) both write `swarm_tick_step` records to the user journal. Total the
routing reads over a window, and count passes stopped at their deadline:

```bash
journalctl --user -t agentihooks-swarm --since "-20 min" \
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

Add `grep "\"slug\": \"$SLUG\""` before `sed` to read one swarm. On 30 October 2026, before the fix, the routing reads
of one twenty minute window took 403 seconds over 272 calls, and both Doctor passes of an hour stopped at the deadline.
A live profile of three of these lookups then took 8.9 seconds, 6.5 of them loading the whole document.

### Tests

`tests/events/test_state_projection.py` checks that the copy and the document commit at the same revision, that a
refused write leaves the copy unchanged, and that an older writer stops every copy being used until a new write
publishes its own. `tests/swarm/test_fencing_reads.py` checks that routing, roster and occupant reads transfer under
10,000 bytes per Redis reply with one or two hundred archived executions, that the archive is still read whole by
`fencing.read`, and that a document rewritten without a copy is read through the fallback with its new hive.
