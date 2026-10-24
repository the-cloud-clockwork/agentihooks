### Fixed

- Retired swarm agents release their execution seats atomically. Each tick also releases orphaned executions after confirming their processes are gone, while preserving live agents and unavailable remote hosts.
