### Changed

- Tick steps no longer carry a hand written order. Each step declares the steps it needs and the agents, tasks and swarm state it reads and writes; a group runs each step after the group steps it needs and otherwise in registration order, and the registry refuses a reader and a writer of the same state in one lane unless one needs the other. Reap comes before the orphan reopen, both before spawn, preempt after reap, and settle after spawn.
