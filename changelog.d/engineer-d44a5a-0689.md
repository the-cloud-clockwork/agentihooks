### Fixed

- Host room counts swap as full only while available memory is also below the swap floor (`swap-floor`, default 4096 MB). Stale swap pages on a host with ample available memory no longer zero the room; swap in use still comes off available memory. The host room line names whether swap held the room or read as stale.
