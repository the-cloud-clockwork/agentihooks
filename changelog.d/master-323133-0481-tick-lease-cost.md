### Fixed

- The controller lease now has its own dedicated, isolated cache key, so renewing it through `FencedRuntime`/`FencedLedger` no longer transfers the whole fencing document (archive included) on every wrapped call; the full document is read or written only when an extension is actually due or the cache is stale. Unlike the first attempt at this tonight, the cache does not reshape the shared `executions` projection, so no other reader of that projection is affected.
- `fencing.write` no longer decodes the fencing document twice to detect a no-op change.
- The swarm tick scheduler now records a delivery after every supervised attempt, not only a clean exit, so a run of deadline or service stops no longer looks like a dead scheduler and revives the retired legacy systemd tick timer alongside it.
