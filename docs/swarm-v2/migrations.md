# Session catalog migrations (SV2-RET-03)

Session catalog schema changes roll out in stages, so old readers, old writers and new writers share one catalog
file while a migration runs and no local work has to stop.

## Stages

`hooks.memory.migrations.Migrator` keeps one row per migration in `schema_migration`: its state, a revision that
every stage compares and increments, the event count when the migration was added, and a durable backfill cursor.

| Stage | Call | Effect |
|---|---|---|
| Schema | opening a `Catalog` | creates `session_event_provenance` if it is missing; nothing else changes |
| Add | `add()`, also run by `startup()` | records the migration and the event count; repeat calls change nothing |
| Backfill | `backfill(revision, batch_size, max_batches)` | fills provenance for events that lack it, one batch per transaction |
| Verify | `verify(revision)` | checks rows, uniqueness, references and field consistency; passes to `verified` or stays `backfilled` |
| Activate | `activate(revision)` | needs `verified` and no rows remaining; consumers may then read provenance |
| Deactivate | `deactivate(revision)` | returns to `verified`; the table and its rows stay |

A stage given a stale revision changes nothing, so an earlier attempt or a second operator never overwrites a newer
result.

## Event provenance

`0001-event-provenance` adds two optional fields per event in a side table keyed by access domain and event id:

- `trust_class`: `original` for every catalog event, which holds transcript evidence and never synthesized memory.
- `project_state`: `attributed` when the event carries a project id, else `unknown`. The project id itself is never
  filled in or guessed.

The new writer, `Catalog.commit`, writes provenance in the event's own transaction. A previous release's writer
inserts events positionally and writes no provenance; its rows count in `migration_rows_remaining` until the
backfill reaches them. `session_event` keeps its columns, so previous readers, including positional `SELECT *`
readers, see the same rows and columns.

The backfill walks events by row id with a cursor stored in the same transaction as each batch. An interrupted
batch rolls back whole; the next run resumes after the last committed batch. Event and chunk identities are never
rewritten. When the cursor reaches the end while rows still lack provenance, it starts again from the beginning.

`verify` reports `migration_rows_remaining`, `duplicate_event_sequences`, `unresolved_sources` (events whose
source generation is not registered), `orphan_provenance` (provenance without its event), `inconsistent_provenance`
and `rows_lost` against the count recorded at add. Every value must be zero.

`Migrator.provenance(domain, event_id)` refuses while the migration is not `active`.

## Vector dimension

A vector stored with a dimension other than its index generation's is a mismatch. `startup()` and `blocked()`
report each affected index generation as `0002-vector-dimension` with its count and never change a vector.

`drop_mismatched_vectors(approval, now)` deletes the mismatched vectors of one access domain and index generation
and queues their chunks for embedding again. It runs only with an `Approval` whose role is `operator`, that names
an actor and `0002-vector-dimension`. A refusal changes nothing and names no scope. A model with another dimension
otherwise gets its own index generation, which leaves the old vectors in place.

## Command line

```bash
python -m scripts.swarm_v2.migrations status CATALOG
python -m scripts.swarm_v2.migrations startup CATALOG
python -m scripts.swarm_v2.migrations backfill CATALOG --revision N [--batch-size 500] [--max-batches 20]
python -m scripts.swarm_v2.migrations verify|activate|deactivate CATALOG --revision N
python -m scripts.swarm_v2.migrations drop-vectors CATALOG --actor A --role operator --domain D --generation G
```

## Compatibility window and rollback

The window opens when a release carrying this writer starts and closes when the migration is `active`. During it,
previous writers and readers keep working unchanged; only consumers that read provenance wait for activation.

Rollback deactivates the projection and keeps `session_event_provenance` and `schema_migration`. Dropping them
needs a reviewed rollback migration; provenance rows are derived from events and can be rebuilt by the backfill,
while events themselves are authoritative and are never deleted by a migration.
