# Launch grants, version 2.0

SV2-IDN-04 binds a worker's identity to authority the controller issued, so an agent cannot claim another
project, task, account or brain by changing request parameters. The interface is
`scripts.swarm_v2.auth_context.LaunchAuthority`; the schema is `urn:swarm-v2:launch-grant`.

## Issue

`issue(slug, execution_id, project_ids=..., brain_id=..., account=...)` returns a signed token for an
execution admitted through `RedisStore.start_execution` (SV2-IDN-02).

- The subject comes from admitted state, never from the caller: `execution_id`, `generation`, `seat_id`
  and `task_id` are read from the execution registry. An execution that is no longer the current attempt
  of its seat is refused (`stale_generation`).
- The controller selects the corpus: a non-empty list of canonical project IDs (SV2-IDN-01, never
  `unknown`), one `brain_id` and one `account`, each validated before anything is written
  (`invalid_request`). The grant carries the projects sorted and unique.
- The grant carries `issuer`, `audience` and `key_id`, issued and expiry times, and a random `grant_id`.
  Lifetime defaults to 300 seconds and is at most 900.
- The token is `v2.<claims>.<HMAC-SHA256>`, with canonical JSON claims and unpadded base64url. The
  signing key is a `LaunchKey` of at least 32 bytes held by the controller; workers never verify grants.
- Each issue records a nonsecret audit row (`launch-grants`): grant, issuer, audience, key ID, execution,
  generation, times and state `issued`. Neither the token nor the key is stored.
- An execution whose task or seat falls outside the identifier grammar gets no grant (`invalid_request`).
- The audit row is written in one watched transaction with the disable switch and the execution
  history: a grant is refused while grants are disabled (`forbidden_scope`) or once its execution is no
  longer the current attempt (`stale_generation`), even when either changes during the issue.

## Register

`register(slug, token, body)` validates the grant before trusting any worker metadata, in this order:

| Check | Error class |
|---|---|
| Token shape, signature, claim set and claim types | `unauthenticated` |
| Schema version other than `2.0` | `invalid_request` |
| Signing key ID, issuer, audience | `unauthenticated` |
| Not yet valid, or at or past expiry | `unauthenticated` |
| Grant for another swarm | `forbidden_scope` |
| Body not an object, names a field outside the bound set, or omits `execution_id` or `generation` | `invalid_request` |
| A body field (`swarm_id`, `execution_id`, `generation`, `seat_id`, `task_id`, `account`, `brain_id`, `project_ids`) whose type or value differs from the grant; `project_ids` must match the grant's sorted list | `forbidden_scope` |
| Grant not issued by this controller, or revoked | `unauthenticated` |
| Grant execution no longer the seat's current attempt | `stale_generation` |
| Execution already registered under another grant | `forbidden_scope` |
| Grants disabled for the swarm | `forbidden_scope` |

Every refusal happens before any registry write. A `GrantRefused` carries `error_class`, the
`operation_id` of the request (`register(..., operation_id)`), echoed only when it is an identifier and
`unknown` otherwise, as the shared contract refusals do; `issue` and `disable` are controller calls and
report `unknown`. It also carries a retry
class: `new_request` for every refusal, since correcting the input takes a new valid request and never
an implicit fallback, and `same_request` for `dependency_unavailable`, raised when a watched transaction
kept conflicting and nothing was written; for `issue` the retry is a fresh issue. `detail()` is the sanitized form; it never contains the token,
the key or another caller's resource. `launch_grant_rejections(store, slug)` counts refusals per error class, and
`launch_grant_rejections_total` sums them.

The last four checks and the write run in one watched transaction over the execution history, the
registrations, the grant audit rows and the disable switch. The transaction writes the registration
(`launch-registrations`) and moves the grant's audit row to `registered`; it never writes execution
records. A retry with the same valid grant, from the same or a restarted controller, returns the stored
`Registration` unchanged and writes nothing. Losing the transport before the commit leaves nothing
written; losing it after the commit leaves the registration, and the retry returns it. A stale attempt
cannot replace a newer generation's registration. `Registration.session_grant()` returns the
SV2-IDN-03 `SessionGrant` for the registered corpus.

## Rollback

`disable(slug)` sets the swarm's disable switch and, in the same transaction, moves every grant still
`issued` to `revoked`, returning their IDs. While disabled, `issue` and every new registration are
refused; a retry of an existing registration still returns it. Registrations, audit rows and execution
records are authoritative history: this module never deletes them and never writes execution records.
`enable(slug)` clears the switch. The preceding local launch path does not call this module, so it is
unaffected either way.

## Limitations

- No live launch path issues or registers grants yet. The registration endpoint and the distributed
  launch that carries the token to a worker belong to the runtime packages; until they land, live hooks
  still pass no grant to SV2-IDN-03 attribution.
- Key provisioning and rotation are not part of this package. A verifier knows one key ID; a rotation
  needs a key set.
- Registrations and audit rows have no retention sweep.
