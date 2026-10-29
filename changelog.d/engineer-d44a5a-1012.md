### Fixed

- `agentihooks init` reschedules the host process guard on the current agentihooks scheduler when both home links exist and no live guard entry is there, and cancels a stranded guard entry for the same home on the local Redis scheduler once the swarm Redis has moved, so the guard runs exactly once again after an endpoint move.
