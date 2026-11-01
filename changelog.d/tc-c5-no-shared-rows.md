### Changed

- Tick steps share no mutable ledger rows: the pass hands steps a read only rows mapping, spawn preparation and reaping work on their own copies of task rows, the ledger view is deep frozen under CI so a step that changes it fails, and a ledger write marks the held view stale so the next step reads a fresh one.
