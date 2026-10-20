### Added

- `python -m scripts.swarm.redis_commands` prints the Redis commands each swarm consumer (controller, tick, ledger server, worker, hooks) reaches from its entry point, with the code that sends each, and the scheduler keys it writes for a deployment mode. Commands are read from the code, with each redis-py method mapped to the command it sends (`incr` sends `INCRBY`), so antoncore CI can check every Redis user against the consumers it serves.
