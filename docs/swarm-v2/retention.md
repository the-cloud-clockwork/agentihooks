# Swarm v2 corpus and checkpoint retention

Package SV2-RET-01. Storage growth is bounded by explicit policy; active recovery and accepted task evidence are never
collected.

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

- `Collector.plan()` is the dry run: the eligibility report, with no delete and no journal entry.
- `Collector.collect(run_id)` writes one `planned` journal entry (eligible ids in delete order, protected counts,
  policy versions, time) before any delete. Manifests are deleted before the objects they name.
- Before each removal the collector re-reads the restore and evidence holds, so an object protected after planning is
  journaled `skipped` with its reason. An object already gone is journaled `absent`.
- An interrupted run resumes with the same `run_id` from `FileJournal`, an fsynced JSON lines file. Settled entries are
  not repeated, decisions are recomputed under the holds current at resume, and a torn final line is ignored.
  The first `planned` entry of a run is never replaced.
- `Collector.delete(object_id)` removes one eligible object and refuses any other with `RetentionRefused`, changing
  nothing.

The collector reads stores through the `Inventory`, `Holds` and `Journal` protocols; a backend adapter implements them.
The hold check before each removal is the guard for a restore already reading; a backend that offers a conditional
delete closes the remaining window between the check and the removal.

## Rollback

Set `SWARM_RETENTION_DESTRUCTIVE=off` (read by `retention.destructive`) and build the collector with
`destructive=False`. Collection then journals the eligibility report and deletes nothing, and `delete` refuses.
References and the journal stay as they are. Committed checkpoint objects and archived evidence are authoritative; do
not delete them as caches. The rehearsal is case B in `tests/test_swarm_v2_retention_cases.py`.

## Evidence

`evidence/SV2-RET-01`: case results A (positive), B (rejection and dry run rollback), C (interrupted run resumed), the
fixture manifest with its input hash, and the package record. The cases run on in memory fixtures with a fixed clock
and are labelled mocked; no live store was collected.
