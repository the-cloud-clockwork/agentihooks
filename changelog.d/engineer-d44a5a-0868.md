### Changed

- Swarm sessions are refused the raw GitHub enqueuePullRequest mutation and are pointed to `agentihooks swarm <slug> merge queue <url>`, so every queue entry from a swarm leaves a journal row. Sessions outside a swarm are unchanged.
