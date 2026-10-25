# Swarm v2 session memory under load

Package SV2-PRF-03. The harness in `tests/performance/session_memory/load.py` replays the workload in
`tests/performance/session_memory/workload.json` through the accepted capture and retrieval path: transcript
collector, execution spool, central archive and ingest, chunk index, bounded embedding worker and scoped search.
The case records are `evidence/SV2-PRF-03/a-result.json`, `b-result.json` and `c-result.json`.

## Workload

- 9 writers append one Claude turn per tick to their own transcript for 16 ticks; every third turn leaves half of
  its assistant line for the next tick. 27 background sessions hold one turn each, so search scopes span 36
  sessions with 32 hex character session ids over three projects: one granted to the searcher, one granted only to
  the wide searcher grant, and one restricted.
- Each execution polls into its own spool every tick and flushes every second tick.
- Duplicate delivery: three lost cursor commits spool the same batch twice, three lost acknowledgements commit
  centrally and are delivered again.
- Overload: the central service is down and embeddings are paused from tick 6 to tick 11.
- Embedding: at most two batches of six chunks per tick.
- Search: on every second tick, the shared term with a one project grant, with a two project grant narrowed to one
  project, and with the two project grant unnarrowed; a request for the restricted project; and an exact phrase
  search for every unseen marker the granted writers wrote.

Ack latency, spool age and embedding backlog age use a fake clock in seconds, so every run of one seed gives the
same record. Search wall time is the only measured clock.

## Results

Both seeds of case A give the same shape; seed 7:

| Measure | Value |
|---|---|
| Transcript lines, authoritative events | 378, 378 |
| Duplicate events counted, not stored | 6 |
| `ingest_ack_latency_seconds` all lines, p50 / p95 / max | 1 / 7 / 9 (n 288) |
| `ingest_ack_latency_seconds` lines written during overload | 4 / 6 / 6 (n 108) |
| Peak spool backlog, peak archive lag | 65 batches, 8 s |
| Peak embedding backlog, peak backlog age | 66 chunks, 5 s |
| Marker visible to search after it was written, p95 | 1 s (n 48, none missed) |
| Results outside the caller's projects | 0 of 1400 shared term hits |
| Backlogs empty at tick | 22 |
| Chunk texts embedded twice | 0 |

`search_latency_seconds` is asserted by `test_search_stays_within_its_wall_clock_budget_under_the_load`: p95 below
0.5 s on the CI runner over the 92 searches of one run. Print the measured values for a seed with
`python -m tests.performance.session_memory.load --seed 7`.

## Rejection

- A spool shrunk to 3000 bytes during the outage refused capture 38 times; no cursor moved on a refusal and the run
  ended complete.
- An uploader that drops backlog older than 3 s reports a lower overload ack latency (p95 3 s instead of 6 s), and
  the run is graded failed: 304 events for 378 lines and 9 sessions whose archive no longer covers the transcript.
- Searches for the restricted project, for it alongside the granted project, for a project outside a narrow grant
  and for another corpus are each refused with the same detail and change no stored state.

## Recovery

Case C restarts every process at tick 9, in the middle of the outage, with 38 spooled batches; all survive. A
cursor saved at tick 1 is refused as stale. Delivering an acknowledged batch again replays it with no change to
stored state, and the same range with other content is refused. Backlogs drain by tick 22 with no chunk text
embedded twice.

## Rollback

Pause optional embeddings and lower ingest concurrency while capture keeps spooling durably. Case C rehearses the
pause for the whole outage. Transcripts, archive entries, archived objects, session events and unsettled spool
batches are authoritative; chunks and vectors are derived.

## Limits

- Writers interleave in one process; no real concurrent processes write one SQLite file.
- Only Claude JSON lines with string content are replayed.
- Postgres, pgvector, the archive bucket, the HTTP service and a real embedding provider are not measured.
