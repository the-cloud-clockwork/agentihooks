### Fixed

- Ledger writes store operation receipts in a thousand fixed slots, so a write rewrites only its own receipt instead of shifting every stored receipt, and hosted replicas refresh only what changed. Receipts stored in the old shape convert on the next write.
