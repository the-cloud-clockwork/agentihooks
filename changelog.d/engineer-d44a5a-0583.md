### Added

- The ledger request metrics command reads individual write timings with
  `--writes`: wall, CPU and lock wait per write, grouped by route, for a fixed
  window ending at `--end`, bounded by `--limit`, with the coverage it holds.
- The ledger server journals request timings beside its ledgers and restores
  them after a reload or restart. Coverage names every span a crash lost and
  keeps ring evictions across restarts. A request still in flight when a
  reload executes is not journaled, and a crash while the ledger folder
  refuses every journal write leaves that span unnamed.
