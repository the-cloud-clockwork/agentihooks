# Swarm v2 corpus and checkpoint retention

Package SV2-RET-01. Storage growth is bounded by explicit policy; active recovery and accepted task evidence are never
collected.

## Completed worker cleanup

A completed worker's Pod stays protected until the controller has both its outcome reference and an archive
watermark covering its final transcript end. Publishing a checkpoint alone does not clear that guard. If the end
exceeds the controller's current watermark, the worker must report the durably archived end on its lease heartbeat.

During a clean drain, `Supervisor.drain` waits for the exporter's final checkpoint. If checkpoint storage is
configured, it validates the checkpoint and its artifacts in that store within the checkpoint time budget, then
passes the exporter's integer `transcript_end` to `Renewer.acknowledge`.

The renewer keeps the largest acknowledged end as `archived`; it starts at zero. A larger end makes a heartbeat due
immediately, and that heartbeat and every later one carry `archive_watermark`. Repeating the same end, or reporting a
smaller one, never lowers it or forces an extra heartbeat. For an end above the previously delivered watermark,
`acknowledge` returns true only after a successful heartbeat response with matching execution and task generation
has confirmed delivery of at least that end.

A failed heartbeat keeps the pending watermark. Drain keeps calling `acknowledge` across the renewer's retry pause
until the controller accepts it, the lease is lost or the checkpoint budget expires. Validation and acknowledgement
share that budget; acknowledgement gets only the time left after validation. A controller refusal or expired lease
ends renewal rather than claiming delivery.

### Configuration

Archive acknowledgement runs automatically with a leased worker and a validated portable final checkpoint. It has
no separate enable flag.

| Setting | Where | Effect |
|---|---|---|
| `checkpoint_remote: true` | Worker launch specification | Uses the controller's scoped checkpoint upload route; the worker sets `AGENTIHOOKS_CHECKPOINT_REMOTE=true` for itself |
| `AGENTIHOOKS_CHECKPOINT_BUCKET` | Controller environment | Stores and validates remote checkpoints in the configured object storage bucket; takes precedence over a local root |
| `AGENTIHOOKS_CHECKPOINT_ROOT` | Controller or worker environment | Configures an absolute checkpoint store directory; a worker with this setting also validates its final checkpoint before acknowledging the transcript end |
| `Budgets.checkpoint` | Supervisor launch integration | Seconds available for the final checkpoint, validation and acknowledgement; defaults to `10` and is recorded as `checkpoint_seconds` in the supervisor launch record |

For a controller using object storage, set its bucket and use the remote option in the worker's existing launch
specification:

```bash
export AGENTIHOOKS_CHECKPOINT_BUCKET=example-checkpoints
```

```json
{
  "checkpoint_remote": true
}
```

The JSON is a launch specification fragment; retain the worker's identity, grant and other required launch fields.
Storage credentials stay in the controller's configured environment. For a filesystem backed integration, configure
an absolute store root in the process that reads or writes it:

```bash
export AGENTIHOOKS_CHECKPOINT_ROOT=/srv/example-checkpoints
```

An integration constructing the supervisor launch can allow more time with its existing budget object:

```python
from scripts.swarm_v2.supervision import Budgets

budgets = Budgets(checkpoint=30)
```

Pass that object as the launch's `budgets` argument. This allows thirty seconds shared by the final checkpoint,
validation and acknowledgement. It does not extend an expired lease. See [checkpoint configuration](checkpoint-restore.md#configuration)
and [storage policy](storage-policy.md) for the checkpoint store and restore requirements.

### Retention example

For a completed attempt whose recorded `transcript_end` is `10`, the retention guard checks the following in order:

1. With no outcome reference, `retention.waiting` returns `waiting_outcome`, even if the archive has arrived.
2. Once the outcome reference exists, a controller archive watermark below `10` returns `waiting_archive`.
3. The supervisor validates the final checkpoint and calls `acknowledge(10)`. The renewer sends
   `"archive_watermark": 10` on an immediate heartbeat. A failed send is retried with the same end.
4. When the controller accepts that heartbeat, `retention.archive_watermark` reads `10` and `retention.waiting`
   returns the empty string. The attempt has passed the outcome and archive guards.

Passing those guards permits the cleanup path to proceed. `Cleanup.run` must still be enabled, hold controller
authority and finish its runtime object removals and retained record. The acknowledgement itself deletes no Pod or
checkpoint object. The corpus collection policy below governs stored objects separately.

A final transcript end of `0` is already covered by the initial archive watermark of `0`. That archive guard can
pass without another accepted heartbeat; the outcome reference is still required.

### Limits and troubleshooting

| Observation | Meaning and action |
|---|---|
| `waiting_outcome` | The final attempt has no outcome reference. Inspect completion reporting first; an archive acknowledgement does not supply the outcome |
| `waiting_archive` after a checkpoint appears | Compare the accepted heartbeat watermark with the final attempt's `transcript_end`; confirm the checkpoint validated in the configured store and the acknowledgement reached the controller |
| Worker log has `lease: renewal_failed` | The heartbeat or credential request failed; `call`, `status` and `retry_seconds` identify the request and pause. Check controller reachability before the shared checkpoint budget expires |
| Worker log has `lease: lost` | `reason` names a refusal, lease expiry or generation change. The worker stops renewing; do not treat a local checkpoint as a delivered archive acknowledgement |
| Final checkpoint is complete but the guard still waits | Checkpoint completion and heartbeat acceptance are separate. Validation can succeed while acknowledgement runs out of budget or loses its lease |
| No portable checkpoint store or failed validation | Drain does not acknowledge the transcript end through this path. Configure the store or correct validation before expecting archive progress |
| Worker has no controller lease | `Unleased.acknowledge` returns true without sending a heartbeat. That local success does not advance any controller watermark |
| Guard clears but runtime objects remain | Inspect the cleanup result and its authority or suspension state; clearing archive retention establishes eligibility, not proof of deletion |

The regression feature `archive-acknowledged-on-heartbeat`, delivered in
[pull request 10901](https://github.com/the-cloud-clockwork/agentihooks/pull/10901), covers immediate and later
heartbeats, unchanged or lower ends, retry after a missed acknowledgement, unleased workers, validated portable
checkpoints and the checkpoint budget. Its completed Pod case drives `Renewer` through `ExecutionsAPI` and verifies
that an end of `10` changes the guard from `waiting_archive` to the empty string. The cases live in
`test_swarm_v2_worker_lease_renewal`, `test_swarm_v2_supervision_runtime` and `test_swarm_v2_executions_api`.
This is regression test evidence; it does not establish a live cluster rollout or Pod deletion.

## Policy

`hooks.memory.retention.Policies.parse` takes a list with at most one policy per corpus, `personal` or `swarm`. Each
policy carries a `version` and a `max_age_seconds` entry for every data class:

| Class | Holds |
|---|---|
| `raw_evidence` | archived transcript records and other original session evidence |
| `native_resume` | checkpoint manifests and native resume bundles |
| `preview` | truncated previews and summaries |
| `vector` | embeddings and other search projections |
| `operational_log` | controller, worker and collection logs |

A max age is a positive number of seconds, or `null` to keep the class. A corpus with no declared policy keeps every
object (`no_policy`); a swarm policy never applies to the personal corpus. An object registered
`retain-until-deleted` never expires by age.

## Protection

`scripts.swarm_v2.checkpoints.retention.Collector` decides every stored object, first reason wins:

| Reason | Kept because |
|---|---|
| `active_restore` | the object is the manifest, or an object it names, of a checkpoint under a registered restore hold |
| `latest_checkpoint` | it belongs to the latest complete checkpoint of an active task |
| `evidence_hold` | an accepted evidence hold names it |
| `unknown_class` | its data class is not one of the five classes |
| `no_policy` | its corpus has no declared policy |
| `not_expired` | its class age is not reached, the class is kept, or it is retain until deleted |
| `referenced` | a retained checkpoint manifest names it |

Everything else is `eligible`. `retention_protected_objects` counts the kept objects by reason.

A restore registers its checkpoint with the holds before it reads the manifest and releases it after the last read.

## Collection

- `Collector.plan()` returns the eligibility report in memory, with no delete and no journal entry.
- `Collector.collect(run_id)` writes one `planned` journal entry (eligible ids in delete order, protected counts,
  policy versions, time) before any delete; with destructive collection off that entry is the recorded dry run.
  Manifests are deleted before the objects they name.
- Before each removal the collector decides that object again against the live inventory and holds, so an object
  protected after planning (a restore, an evidence hold, a newly completed checkpoint naming it) is journaled `skipped`
  with its reason, and an object already gone is journaled `absent`. Each removal costs one full decision pass.
- An interrupted run resumes with the same `run_id` from `FileJournal`, an fsynced JSON lines file. Settled entries are
  not repeated and a torn final line is ignored. The first `planned` entry of a run is never replaced.
- `Collector.delete(object_id)` removes one eligible object and refuses any other with `RetentionRefused`, changing
  nothing.

The collector reads stores through the `Inventory`, `Holds` and `Journal` protocols; a backend adapter implements them.
`DocumentInventory` is the one shipped adapter: a JSON document with `policies`, `objects`, `checkpoints` and `holds`,
driven by `python -m scripts.swarm_v2.checkpoints.retention plan|collect <document> --journal <file> [--run-id <id>]`,
which prints the report as JSON.
It takes no grant: the caller, the store owner's process, holds the delete authority and the scope of the inventory it
passes in. The decision before each removal is the guard for a restore already reading; a backend that offers a
conditional delete closes the remaining window between that decision and the removal.

## Rollback

Set `SWARM_RETENTION_DESTRUCTIVE=off` in the collector's environment; a collector built without an explicit
`destructive` argument reads it through `retention.destructive_enabled`. Collection then journals the eligibility
report and deletes nothing, and `delete` refuses.
References and the journal stay as they are. Committed checkpoint objects and archived evidence are authoritative; do
not delete them as caches. The rehearsal is case B in `tests/test_swarm_v2_retention_cases.py`.

## Evidence

`evidence/SV2-RET-01`: case results A (positive), B (rejection and dry run rollback), C (interrupted run resumed), the
fixture manifest with its input hash, and the package record. The cases run on in memory fixtures with a fixed clock
and are labelled mocked; no live store was collected.
