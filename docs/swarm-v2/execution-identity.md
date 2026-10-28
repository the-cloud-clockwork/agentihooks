# Execution identity metadata, version 2.0

SV2-IDN-02 extends AgentRecord with execution_id, generation, runtime_backend and runtime_target. Canonical names continue to come from NameRegistry. A seat, its memories and its task lineage survive changes of execution identity or canonical process name. These records provide identity metadata only; they carry no grant and are not evidence of worker liveness or permission to mutate a task.

RedisStore.start_execution admits an incarnation before runtime creation. Its input is a named AgentRecord without an execution ID or generation. The caller supplies the exact previous_execution_id for a replacement, or an empty predecessor for the seat's first tracked attempt. The store allocates a UUID and increments the seat's execution generation in one watched transaction. Generations describe executions of a seat; task ownership generations and controller epochs belong to the later fencing package.

A Kubernetes target contains pod_namespace, pod_name and an optional pod_uid. The UID is bound once through put_agent after runtime creation or reconciliation; all other target fields are immutable. Local targets may carry server_id, process_namespace and a positive pid. A missing local target preserves compatibility for metadata admission before the local runtime has started. Different Pods may share a display name; execution lookup accepts only the immutable execution ID. Reusing any previously recorded Pod UID is refused. UUID collisions, changed incarnation fields and stale predecessor or update requests are also refused. execution_identity_conflicts_total counts refusals per swarm without granting authority.

RedisStore.execution retrieves an exact attempt. RedisStore.executions returns the seat's ordered history. RedisStore.execution_occupants reconstructs the latest recorded incarnation per seat from persisted attempts, even when the current agents projection has been lost. This projection includes the last observation, including terminal states; the controller must consult runtime observations and execution authority before taking actions. Attempt records survive drop_agent, controller reconnection and the existing export/restore snapshot path. Latest attempt metadata can change through put_agent; superseded attempt records cannot be updated by that interface.

The preexisting local writer remains available for AgentRecord rows with default metadata. A remote record or a name already tracked in execution history must use admitted identity. Runtime adapter wiring and authenticated grants are separate dependent work packages; this package creates no Pods, processes or task leases.

Rollback: stop distributed launches before reverting the writer. Keep executions and seat memory as authoritative data. Old local readers can project the preceding AgentRecord fields; current readers also accept records that lack the added fields. The isolated rollback rehearsal checks this projection and verifies export/restore preserves all three attempts. Revert code through Git; do not delete attempt records to simulate rollback.

The agents hash remains a projection with the preceding record shape. New readers obtain additive identity from the executions hash in a consistent read transaction. This preserves the preceding strict dataclass decoder without requiring an old binary to understand the four added fields. Local writes watch both projection and execution history, so concurrent admission cannot be overwritten by an unadmitted legacy write.

## Parallel launches in one process

The swarm tick launches every engineer and CI seat of a pass at once, one thread per launch. Each launch admits its execution through `RedisStore.start_execution`, then binds its pane and updates its record. Each of these writes watches the swarm's `executions` hash and retries when another writer changes it first. A write gives up after five watched attempts (`WRITE_ATTEMPTS`).

Before this change, the threads of one batch raced each other on the same hash. With six to eight launches in one pass, a launch could lose all five attempts to its siblings. Its spawn then failed, the tick reopened the task, and the Doctor reported a `failed-spawn/<task>` finding.

`ExecutionRegistry` now holds one lock per swarm for each history write:

| Method | Store entry point | Error when five attempts are lost |
|---|---|---|
| `start` | `RedisStore.start_execution` | `execution history kept changing; admission was not committed` |
| `update` | `RedisStore.put_agent` for an admitted record | `execution history kept changing; update was not committed` |
| `set_pane` | `ExecutionRegistry.set_pane` | `execution history kept changing; pane was not committed` |
| `put` | `RedisStore.put_agent` | `agent registry kept changing; update was not committed` |

- Writes for the same swarm in one process run one at a time. A sibling launch waits for an admission in flight instead of spending its attempts against it.
- Writes for different swarms do not wait for each other. The lock is keyed by the swarm slug.
- The lock is reentrant. `put` on an admitted record calls `update` while it holds the lock.
- The lock lives in process memory. A writer in another process, such as a second tick or an `agentihooks swarm` command, still meets the watched transaction and its five attempts. The error can still appear when another process writes the same swarm's history that often.

Nothing is configured: the lock applies to every swarm and every launch path that writes through `ExecutionRegistry`.

### Reading a failed spawn

A launch that still runs out of attempts appears in the tick journal as:

```text
<slug>: spawn failed for <task id>, task <task id> reopened: execution history kept changing; admission was not committed
```

```bash
# Spawn failures from execution history contention in the last day
journalctl --user -t agentihooks-swarm --since "1 day ago" \
  | grep "spawn failed for" | grep "execution history kept changing"
```

After this change, a line like this means a writer outside the tick process changed the history during the launch. Look for a writer in another process before the failure time; the tick's own parallel launches no longer cause it. The reopened task launches again on a later pass. Never edit the `executions` hash by hand to clear a failure.

The tests in `tests/swarm/test_execution_concurrency.py` cover this behavior. They check that a sibling admission, update and pane write wait for an admission in flight, that another swarm writes during it, and that each of the four writes holds the lock of its own swarm.
