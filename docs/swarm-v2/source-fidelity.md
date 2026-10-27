# Source fidelity audit (SV2-MIG-02)

Before legacy transcript service (agentibridge) history is imported, each historical session is classified by
what survives of it. The audit is code: `hooks.memory.source_audit.audit_source` classifies one session and
`hooks.memory.source_audit.SourceAudit` holds the audit record across runs. It reads; it never rebuilds the legacy
service, its collector or its stores.

## Classes

| Class | Survives | Import confidence |
|---|---|---|
| `full_raw` | the provider's raw JSONL transcript | `lossless` |
| `preview_only` | the legacy Redis entry preview, no raw transcript | `lossy` |
| `vector_only` | legacy vector rows only | `fragment` |
| `missing` | nothing | `none` |

`SourceAudit.certify` gives a `lossless` import label only to a `full_raw` source. A preview-only, vector-only or
missing source is refused with `not_lossless`. `SourceAudit.report` lists the four classes separately and counts
them in `source_fidelity_classes`.

## Legacy truncation measured

Read from the retired service's store, collector and embedding code:

| Legacy store | Truncation | Measured as |
|---|---|---|
| Redis entry list | each entry's content cut to 500 characters | `capped` (raw known) or `possibly_capped` (preview only) |
| Redis entry list | list trimmed to the newest 500 entries | `trimmed_head` range, or `possibly_trimmed` |
| Incremental collector | a read resumed at a stored offset discards the next full line | `incremental_offset_gap` range |
| Incremental collector | entries written after the last collection | `uncollected_tail` range |
| Vector rows | text preview cut to 1000 characters | rows compared at that cap |
| Vector rows | `chunk_idx` numbered over the trimmed preview, upserted in place | `renumbered` rows and `orphans` |

When no preview entry matches the raw transcript, its ranges are `unplaced`. Missing ranges are entry index ranges in the raw transcript, so they exist only for `full_raw` sources. A
preview-only source reports `missing_ranges: "unknown"`; the audit never fills absent text from a preview or a
vector row. Limits are `LegacyLimits` (500, 500, 1000 by default).

## Reruns

Each classification keeps the hashes of the raw bytes, preview and vector rows it was made from. A rerun after
more raw files appear upgrades only the sources whose class rose; unchanged sources replay. A lower observation,
such as a raw file gone again, is reported `kept` and never downgrades the held classification. A run that would
change the record at or below its current revision is refused as `stale_revision` with nothing written; a run in
which every source replays is accepted as a no op. A run naming one source twice is refused as `duplicate_source`.

## Rollback

Revert by deleting the classification record, the projection, while keeping the source hashes in the package evidence.
The raw files, Redis previews and vector rows are authoritative and are never deleted by the audit.
