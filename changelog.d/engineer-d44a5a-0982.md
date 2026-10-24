### Added

- The delivery tiers reference documents how `Gate — Required` grades the merge queue fail fast watcher: a lost or cancelled watcher with no reported failure keeps a passed merge group green, and a reported failure still fails the gate.
