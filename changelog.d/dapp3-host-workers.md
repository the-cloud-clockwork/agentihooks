### Fixed

- The swarm controller keeps a lane agent placed on another hive live while its own heartbeat is fresh: the reap pass refreshes its claim instead of dropping it as a gone pane, and the verify pass keeps it when it reads no process for it. A task is no longer handed to a second engineer while the first one works on the workstation.
