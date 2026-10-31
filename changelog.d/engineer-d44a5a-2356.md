### Fixed

- Rebuild a stale inbox open index and back fill pending mail in bounded Redis batches, so a large mailbox no longer sends one oversized watch or read.
