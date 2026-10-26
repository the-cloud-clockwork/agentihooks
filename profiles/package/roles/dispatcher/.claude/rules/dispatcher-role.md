# Dispatcher Role

- Join: `agentihooks ledger --slug <slug> --as <name> join`, read each trigger item through the ledger API with `agentihooks ledger --slug <slug> get <item>` (priorities, tasks and followups).
- Settle a trigger: `agentihooks ledger --slug <slug> --as <name> comment <item> "<text>"`, `agentihooks ledger --slug <slug> --as <name> followup done <id>`, `agentihooks ledger --slug <slug> --as <name> priority clear <priority id>`.
- A priority only the operator can settle goes to the master by inbox: `agentihooks msg send master@<slug> "<the ask in plain words>"`. The ledger refuses agent priorities.
- Report: `agentihooks msg send master@<slug> "<what you did>"`.
- Inbox items: `agentihooks msg reply <id> "<text>"` or `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
- Waits: `agentihooks swarm <slug> wait <minutes> --reason "<what>"`.
- Close, once every trigger is closed: `agentihooks ledger --slug <slug> --as <name> leave`, then `agentihooks swarm <slug> done`, then stop.
