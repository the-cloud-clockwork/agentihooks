# CI Role

- Join the crew: `agentihooks ledger --slug <slug> --as <name> join`, then watch `agentihooks ledger watch <slug> --as <name>`.
- Record the work as it lands: `agentihooks swarm <slug> pr <url>`, `agentihooks ledger --slug <slug> --as <name> comment phases/<id> "<what landed>"`.
- The next bottleneck with its measured number: `agentihooks ledger --slug <slug> --as <name> followup add "<text>"`.
- Waiting on a pipeline or rollout: `agentihooks swarm <slug> wait <minutes> --reason "<what>"`.
- Close: `agentihooks ledger --slug <slug> --as <name> leave`, then `agentihooks swarm <slug> done --pr <url>`.
