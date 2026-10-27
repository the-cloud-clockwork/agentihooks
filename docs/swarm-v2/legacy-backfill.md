# Legacy backfill (SV2-MIG-03)

`hooks.memory.legacy_backfill.backfill` imports legacy session history into the session corpus. It reads legacy
transcripts, previews and vector rows as data and writes only the session catalog and its own backfill ledger; it
does not rebuild the retired transcript service.

## Source selection and identity

Sources are grouped by provider, installation and native session id. Each group is classified with the SV2-MIG-02
source audit and imported once from its highest class: `full_raw`, then `preview_only`, then `vector_only`. A
`missing` session is reported, never imported.

| Situation | Result |
|---|---|
| The same transcript in several folders | one import; every folder is kept in `source_references` |
| The same native id on two installations | two sessions, `corpus:provider:installation:native_id` |
| Two copies at the top class with different content | the session is quarantined as `conflicting_source_content` with both digests |
| Raw bytes that do not match their recorded digest | that copy is quarantined as `refused_source` |
| A catalog event already held at a backfill event identity with other content | the session is quarantined as `event_identity_conflict`; the held event is unchanged |

A raw session uses the importer's generation, `import:<raw sha256>`, so a session `hooks.memory.importer` already
committed replays with no second copy. Preview and vector sessions use `legacy:<digest>`. Records keep their original
timestamps as `source_time`; a project comes only from an absolute alias, and events without one count as unknown
attribution apart from verified alias events.

Each imported session is recorded with its migration run, importer version, source digest, fidelity and confidence.

## Batches and recovery

Sessions are sorted and cut into batches of `batch_size`. A run id is bound to a digest of its planned work; reusing
it with other sources is refused as `plan_mismatch`. The checkpoint moves one batch at a time and only from the batch
the runner read, so an older runner is refused as `stale_checkpoint`. A run stopped by `limit` reports `partial`, a
catalog outage reports `interrupted`; both report `legacy_import_remaining_records` and the remaining sessions, and a
rerun with the same run id resumes at the checkpoint. Sessions already recorded are skipped, so no event is written
twice. A finished run with missing or quarantined sessions reports `completed_with_gaps`, never `completed`.

## Command line

```bash
python -m hooks.memory.legacy_backfill run CATALOG LEDGER PLAN RUN_ID --corpus C --project P [--limit N]
python -m hooks.memory.legacy_backfill report LEDGER RUN_ID
python -m hooks.memory.legacy_backfill deactivate LEDGER RUN_ID
python -m hooks.memory.legacy_backfill projection CATALOG LEDGER CORPUS
```

`PLAN` is JSON with `corpus`, `aliases`, `batch_size` and `sources`; a source's `raw` names a transcript file beside
the plan. The command runs in the operator role: the grant's principal is `operator`, its corpus `--corpus` and its projects the
`--project` values. A refused run prints its error and exits 1.

## Rollback

`BackfillLedger.projection` lists the active backfilled sessions of a corpus with their catalog events; it is the read
path consumers cut over to in SV2-MIG-04. `deactivate` marks one run's sessions inactive, so they leave the projection. Catalog events, the backfill ledger rows and every source
file stay; the sources are authoritative and are never deleted by the backfill.
