# Shared contract conformance (SV2-VAL-01)

Every repository that produces or consumes a Swarm v2 transport contract runs the same case matrix against its own admission endpoint and publishes the result for its commit.

## Inputs

- `docs/swarm-v2/schemas/`: the JSON Schema 2020-12 contracts and `compatibility.json`.
- `tests/contracts/swarm_v2/fixtures/matrix.json`: one entry per case, generated from the contract fixtures by `conformance.build_matrix`.
- `tests/contracts/swarm_v2/conformance.py`: the runner. It needs only `jsonschema` and `referencing` and imports no producer package, so a consumer repository vendors it with the schemas and the matrix.

## Case families

| Family | Expected answer |
|---|---|
| `current`, `previous-minor`, `newer-minor`, `extension`, `newer-generation` | accepted; the stored record equals the submitted record, unknown optional fields included |
| `replay` | the second submission answers `replayed` and stored state is unchanged |
| `invalid`, `unknown-authority`, `future-major`, `missing-authority`, `previous-minor-missing-authority` | `invalid_request`, retry `new_request` |
| `reused-operation` | `revision_conflict`, retry `new_request` |
| `older-generation`, `held-generation` | `stale_generation`, retry `never` |
| `outside-grant` (contracts whose authority names a project, brain or swarm) | `forbidden_scope`, retry `new_request` |

The error contract has no authority and no admission endpoint; its cases validate the record against `error.json` directly.

A case passes only when every check holds: a 2xx status for an accepted record and a 4xx status for a refusal, the expected `state` or `error` and `retry`, a refusal body that is a valid error envelope, no copy of the caller bearer in the answer, unchanged stored state after a refusal or replay, and a stored record equal to the accepted one. An HTTP success never excuses a missing refusal.

## Running against a repository

A target is a factory taking a fresh working folder and returning an object with:

- `submit(contract, doc, grant) -> (status, body)`
- `snapshot() -> dict`, the stored state used to prove a refusal wrote nothing
- `stored(contract, operation_id) -> dict | None`

```
python conformance.py <schemas> <matrix.json> <module:factory> <repository> <commit> <workdir>
```

It prints `swarm-v2-conformance-results/1` JSON naming the repository, commit, schema and matrix digests, `contract_cases_passed`, `contract_cases_failed` and each case's failures, and exits non-zero on any failure. The agentihooks target is `tests/contracts/swarm_v2/agentihooks_target.py`, over `scripts.swarm_v2.contracts.admit`.

## Rollback

Keep the previous compatible schema active until every required consumer qualifies: lower `write_minor` in `compatibility.json` so producers write the previous minor while readers keep accepting both. Admission records are authoritative and are never deleted as caches.
