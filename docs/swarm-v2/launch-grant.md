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
- The controller selects the corpus: a non-empty set of canonical project IDs (SV2-IDN-01, never
  `unknown`), one `brain_id` and one `account`, each validated before anything is written.
- The grant carries `issuer`, `audience` and `key_id`, issued and expiry times, and a random `grant_id`.
  Lifetime defaults to 300 seconds and is at most 900.
- The token is `v2.<claims>.<HMAC-SHA256>`, with canonical JSON claims and unpadded base64url. The
  signing key is a `LaunchKey` of at least 32 bytes held by the controller; workers never verify grants.
- Each issue records a nonsecret audit row (`launch-grants`): grant, issuer, audience, key ID, execution,
  generation, times and state `issued`. Neither the token nor the key is stored.

## Register

`register(slug, token, body)` validates the grant before trusting any worker metadata, in this order:

| Check | Error class |
|---|---|
| Token shape, signature, claim set | `unauthenticated` |
| Schema version other than `2.0` | `invalid_request` |
| Signing key ID, issuer, audience | `unauthenticated` |
| Not yet valid, or at or past expiry | `unauthenticated` |
| Grant for another swarm | `forbidden_scope` |
| Grant not issued by this controller | `unauthenticated` |
| Body not an object, names a field outside the bound set, or omits `execution_id` or `generation` | `invalid_request` |
| Any body field (`swarm_id`, `execution_id`, `generation`, `seat_id`, `task_id`, `account`, `brain_id`, `project_ids`) that differs from the grant | `forbidden_scope` |
| Grant revoked | `unauthenticated` |
| Grant execution no longer the seat's current attempt | `stale_generation` |
| Execution already registered under another grant | `forbidden_scope` |

Every refusal happens before any registry write. Messages name the failed check and never contain the
token, the key or another caller's resource. `launch_grant_rejections(store, slug)` counts refusals per
error class; `launch_grant_rejections_total` sums them.

A registration is written in one watched transaction with the execution history and the grant's audit
row, which moves to `registered`. A retry with the same valid grant, from the same or a restarted
controller, returns the stored `Registration` unchanged and writes nothing. A stale attempt cannot
replace a newer generation's registration. `Registration.session_grant()` returns the SV2-IDN-03
`SessionGrant` for the registered corpus.

## Rollback

`revoke_outstanding(slug)` moves every grant still `issued` to `revoked` and returns their IDs; a revoked
grant can no longer register. Registrations and audit rows are authoritative history and stay readable;
nothing deletes them. Reverting the code leaves the preceding local launch path untouched, because no
existing launch path calls this module.

## Limitations

- No live launch path issues or registers grants yet. The registration endpoint and the distributed
  launch that carries the token to a worker belong to the runtime packages; until they land, live hooks
  still pass no grant to SV2-IDN-03 attribution.
- Key provisioning and rotation are not part of this package. A verifier knows one key ID; a rotation
  needs a key set.
- Registrations and audit rows have no retention sweep.
