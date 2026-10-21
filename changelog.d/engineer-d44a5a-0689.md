### Fixed

- Host room counts swap as full only while available memory is also below the swap floor (`swap-floor`, default 4096 MB). Above the floor, stale swap no longer zeroes the room outright; room stays available memory less swap in use, at the per agent figure. The host room line names whether swap held the room or read as stale.
