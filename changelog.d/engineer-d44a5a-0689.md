### Fixed

- Host room counts swap as full only while available memory is also below the swap floor (`swap-floor`, default 4096 MB), so stale swap pages on an idle host no longer hold every spawn; the host room line names whether swap held the room or read as stale.
