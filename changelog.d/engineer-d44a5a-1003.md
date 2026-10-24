### Added

- The swarm pillar documents how the `ready` merge pass handles a pull request GitHub marks unmergeable: the live owner notice, the fresh read before a dequeue, the `recover` wait that queues an unchanged green head again with a doubling backoff across a timer restart, the `requeue` ledger alerts, the reopen for a real conflict, the queue journal fields, and the before and after counts.
