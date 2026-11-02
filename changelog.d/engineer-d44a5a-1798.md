### Fixed

- A finished hive agent whose hive stopped reporting is dropped once the hive heartbeat misses the ninety second live window, whatever its own last heartbeat, instead of failing its retire every tick for the full ten minute lost window and raising a retire failed finding. Working agents keep the ten minute window.
