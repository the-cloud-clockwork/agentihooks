# Search consumer cutover (SV2-MIG-04)

The replacement session tools take over a search consumer only after a shadow replay shows that every expected
source range is found by search and expanded to its records under the consumer's own grant. Ranking scores are
never compared. Package record: `evidence/SV2-MIG-04/result.json`.

## Interfaces

| Interface | Behavior |
|---|---|
| `hooks.memory.cutover.shadow(tools, grant, questions, now)` | Runs each question through `session_search`, expands every hit with `session_records` under the same grant, and counts an expected range recalled only when each of its sequences was expanded. Reports `shadow_search_source_recall`, unresolved ranges, unexpandable results and results outside the grant; `accepted` only at full recall with no result outside the grant. A question with no expected range raises. |
| `ConsumerRoutes.qualify` | Runs the shadow replay and stores the proof, bound to the consumer and the grant, under a deterministic operation id. |
| `ConsumerRoutes.switch` | Moves a consumer to `session.v1` or `local.v1` with compare and set on the route revision, only with an accepted proof for that consumer and grant. Replaying the same switch returns the same route. |
| `ConsumerRoutes.rollback` | Restores the route held before the given revision as a new revision. Capture, the catalog and stored proofs are untouched. Replaying the same rollback returns the same route. |
| `ConsumerRoutes.call` | Dispatches a consumer request to the endpoint its route names, refusing an endpoint the caller does not serve and a request whose protocol major differs from the endpoint version. Expiring aliases stay enforced by `hooks.memory.api.records`, removal deadline `2027-01-31T00:00:00Z`. |

Supported endpoint versions: `local.v1` (the last qualified local path, the default route) and `session.v1`
(session tools protocol 1 requests, answered by the protocol 2.0 tools).

## Outcomes

- Observed: the A, B and C case outputs in `evidence/SV2-MIG-04/` and the CI runs named in the package record.
- Accepted: shadow proofs stored by `ConsumerRoutes.qualify` with state `accepted`.
- Committed: route revisions written by `ConsumerRoutes.switch` and `ConsumerRoutes.rollback`.
- Externally verified: none. No deployed consumer was switched.

## Rollback

Run `ConsumerRoutes.rollback` with the current revision. The consumer returns to its previous endpoint while
capture keeps writing, and the shadow proof stays readable through `ConsumerRoutes.evidence`. A switch prepared
against the earlier revision is refused, so a stale writer cannot undo the rollback. Case C rehearses this after a
restart of the route store.

## Limitations

- The questions are reconstructed from repository history over the sanitized session tools fixture. A replay of
  live historical sessions needs read access to the hosted catalog and is not part of this record.
- No consumer outside agentihooks is routed through `ConsumerRoutes` yet; each consumer moves when it adopts it.
- Route changes are authorized by the caller's grant. The route store has no separate operator role.
