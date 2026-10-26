# Revocation contract (SV2-RET-02)

A session that leaves the `retained` state is revoked for every reader. This page states what each surface does
and how consumers converge after downtime.

## Tombstones

`Catalog.set_state` writes a row to `session_tombstone` in the same transaction as every state change, a
regrant to `retained` included: a per
access domain sequence, the session, its new state and revision, and the time it was recorded. A stale revision
changes nothing and writes no tombstone. `Catalog.tombstones(domain, after)` reads them in sequence order.

## Consumers

`hooks.memory.revocation.Propagator` holds one durable cursor per access domain and consumer in
`revocation_cursor`. `replay(domain, consumer, now)` applies each tombstone after the cursor in its own
transaction, which also advances the cursor and records the lag in `revocation_applied`. A step whose cursor has
already moved applies nothing, so an earlier attempt or a second replayer never repeats or overwrites a result.
`catch_up(domain, now)` replays every consumer. Each applied step returns a notice naming the affected evidence
references, which brain consumers act on.

| Consumer | On a session that is no longer retained | Notice references |
|---|---|---|
| `search` | removes its search chunks, chunk generations, chunk cutover, vectors and embedding work | removed chunk ids |
| `packs` | drops every cached context pack that cites the session | dropped pack ids |
| `lessons` | keeps only support that is still retained; redacts the text when none remains | touched lesson ids |

A tombstone for a session that is retained again when it is replayed removes nothing: the consumer converges to
the current state, not the state at the time of the tombstone.

## Reads

Every read checks the current session state, so a consumer that is behind still refuses revoked evidence.

- Search and the `search_sessions` and `search_semantic` aliases search only retained sessions.
- Record range expansion and the `get_session_segment` alias refuse a citation to a session that is not retained.
- `Propagator.pack` withholds a pack that cites any session that is not retained, with one detail for every refusal.
- `Propagator.lesson` returns only retained support and reports the lesson redacted when none remains.

`Propagator.exposures(domain, session_uid)` counts every surface that still holds the session. A propagation
that deleted only the vector rows leaves the search chunk previews and the cached pack, and fails this check.

## Authoritative data and rollback

Catalog events, sources and archive objects stay: deletion of authoritative objects follows the retention policy
(SV2-RET-01). Projections removed here are rebuilt from those objects through the normal chunk build, validation
and cutover once a session is retained again. A redacted lesson stays redacted and a dropped pack stays dropped
after a regrant; restoring revoked access is never a rollback step. Rewinding the consumer cursors and replaying
every tombstone keeps all reads refused and changes nothing that is already converged.

## Metric

`revocation_propagation_lag_seconds`: the time between a tombstone being recorded and a consumer applying it,
per consumer and tombstone, read with `Propagator.lags(domain)`.

## Limits

- Scope changes are session state changes only; a project grant narrowed on a running caller is enforced at read
  time by the existing grant checks and emits no tombstone.
- Consumers run when called; no scheduler in this package calls `catch_up` on a timer.
