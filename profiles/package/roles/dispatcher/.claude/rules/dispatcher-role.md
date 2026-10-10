# Dispatcher Role

- Join: `agentihooks ledger --slug <slug> --as <name> join`, read the ledger with `agentihooks ledger --slug <slug> show`.
- Settle a trigger: `agentihooks ledger --slug <slug> --as <name> comment <item> "<text>"`, `agentihooks ledger --slug <slug> --as <name> followup done <id>`, `agentihooks ledger --slug <slug> --as <name> priority clear <priority id>`.
- Report: `agentihooks msg send master@<slug> "<what you did>"`.
- Inbox items: `agentihooks msg reply <id> "<text>"` or `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
- Waits: `agentihooks swarm <slug> wait <minutes> --reason "<what>"`.
- Close, once every trigger is closed: `agentihooks ledger --slug <slug> --as <name> leave`, then `agentihooks swarm <slug> done`, then stop.
