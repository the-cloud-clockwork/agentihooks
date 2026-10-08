# Engineer Role

- Join the crew: `agentihooks ledger --slug <slug> --as <name> join`, then watch `agentihooks ledger watch <slug> --as <name>` and acknowledge operator events with `agentihooks ledger --slug <slug> --as <name> ack`.
- Record the work as it lands: `agentihooks swarm <slug> issue <url>`, `agentihooks swarm <slug> pr <url>`, `agentihooks ledger --slug <slug> --as <name> comment phases/<id> "<what landed>"`.
- Follow ups: `agentihooks ledger --slug <slug> --as <name> followup add "<text>"`.
- Waits and blockers: `agentihooks swarm <slug> wait <minutes> --reason "<what>"`, `agentihooks swarm <slug> block "<why>"`.
- Inbox items: `agentihooks msg reply <id> "<text>"` or `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
- Merge: `gh pr merge` only queues the pull request, and GitHub refuses a push while it sits in the queue. To fix it, dequeue it first (the `dequeuePullRequest` command in the swarm-engineer skill), then push the fix, and once its checks pass queue it again.
- Close: `agentihooks ledger --slug <slug> --as <name> leave`, then `agentihooks swarm <slug> done --pr <url>`.
