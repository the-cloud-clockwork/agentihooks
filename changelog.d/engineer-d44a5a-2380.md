### Fixed

- A scheduled swarm tick starts the detached refresh job before its spawn and reap work, so a pass that overruns and stops there still replays deferred merge queue priorities and runs the other refreshes.
