### Fixed

- A finished hive agent whose hive stopped reporting is dropped once the hive heartbeat misses the ninety second live window, instead of failing its retire every tick for the full ten minute lost window and raising a retire failed finding.
