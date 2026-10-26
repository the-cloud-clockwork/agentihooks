# Swarm v2 backup manifests and restore drills

Package SV2-RET-04. A backup set counts only once an isolated restore drill has rebuilt state from the backup alone
and answered a historical query; an upload by itself is never accepted.

## Capture

`scripts.swarm_v2.backup.capture(sources, backup_store, now)` copies one coordinated set into a content addressed
backup store and returns its set id, the sha256 of its manifest:

| Component | Source | Notes |
|---|---|---|
| `catalog` | the session catalog file | copied with the SQLite backup API; chunk and vector projection tables are dropped from the copy |
| `ledger` | ledger export bytes | |
| `coordination` | swarm coordination snapshot bytes | |
| `brain` | brain backup export bytes | |
| objects | every archive object the catalog manifest names, plus every checkpoint object | each digest is `present` or carries the read error |

Each component records its digest, byte size, `version` and encryption `key_id`. The manifest records `format`
(`1.0`), `captured_at`, the object inventory, the checkpoint object lists per task and `omitted_projections`.
A source object that is missing or altered is recorded with its error and never copied or replaced.

## Restore

`restore(backup_store, set_id, root, keyring)` refuses, before it writes anything, a manifest whose format major
differs from this release and any component whose `key_id` is not in the keyring. The target must be empty, or
hold a `restore.json` marker naming the same set; a rerun into that folder resumes, and content addressing keeps
object copies idempotent. Every object is read back through its digest. Objects missing from the set are returned as
`irrecoverable`.

`rebuild(catalog, projections, now)` rebuilds the omitted chunk and vector projections for every retained session
from the restored events.

## Drill

`drill(backup_store, set_id, Drill(root, keyring, projections, query, task_id, clock))` restores, rebuilds, runs the
agreed historical query against the restored catalog and objects, and reads every object of the designated task
checkpoint. Its report carries `backup_restore_verification_status`:

| Status | Meaning |
|---|---|
| `verified` | the query answered, the checkpoint recovered, nothing irrecoverable |
| `partial` | the query answered and the checkpoint recovered; the listed objects are irrecoverable |
| `failed` | a component failed its digest, the query failed or the checkpoint did not recover |

The report also records `recovery_point` (the set's `captured_at`) and `recovery_time_seconds` measured by the
drill clock. Both are observations of that drill, not guarantees.

## Registry and retention

`Registry(path)` keeps one entry per set. `record` starts it `uploaded`. `verify(set_id, attempt, report)` stores
the drill status only for a higher attempt; the same attempt with the same report is a no-op, and an older or
conflicting attempt is refused. `accepted` is true for `verified` and `partial` sets. `preserved` names the newest
accepted set, which retention must keep. `retention_allowed` is true only while the newest set is `verified`, so a
newer unverified, partial or failed set stops destructive retention until coverage is restored.

## Limitations

- Encryption happens in the storage layer; this module records key ids and checks them against a keyring.
- The evidence is fixture-only: local folders and a SQLite file stand in for the buckets, the central database, the
  hosted ledger, Redis and the brain export. No deployed backup has been restored.
- No scheduler runs capture or drills yet.
