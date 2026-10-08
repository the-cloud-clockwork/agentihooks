# Engineer Role

- Join the crew: `agentihooks ledger --slug <slug> --as <name> join`, then watch `agentihooks ledger watch <slug> --as <name>` and acknowledge operator events with `agentihooks ledger --slug <slug> --as <name> ack`.
- Record the work as it lands: `agentihooks swarm <slug> issue <url>`, `agentihooks swarm <slug> pr <url>`, `agentihooks ledger --slug <slug> --as <name> comment phases/<id> "<what landed>"`.
- Follow ups: `agentihooks ledger --slug <slug> --as <name> followup add "<text>"`.
- Waits and blockers: `agentihooks swarm <slug> wait <minutes> --reason "<what>"`, `agentihooks swarm <slug> block "<why>"`.
- Inbox items: `agentihooks msg reply <id> "<text>"` or `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
- Merge: on a base branch with a merge queue, a merge only queues the pull request, so do not merge there: queue it on green checks with `agentihooks swarm <slug> merge queue <url>` and read its place with `agentihooks swarm <slug> merge state <url>`. GitHub refuses a push while it sits in the queue. To fix it, dequeue it first with `agentihooks swarm <slug> merge dequeue <url>`, then push the fix, and once its checks pass queue it again.
- Close: `agentihooks ledger --slug <slug> --as <name> leave`, then `agentihooks swarm <slug> done --pr <url>`.
