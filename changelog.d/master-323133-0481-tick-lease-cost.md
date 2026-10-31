### Fixed

- `fencing.write` no longer decodes the fencing document twice to detect a no-op change.
- The swarm tick scheduler now records a delivery after every supervised attempt, not only a clean exit, so a run of deadline or service stops no longer looks like a dead scheduler and revives the retired legacy systemd tick timer alongside it.
