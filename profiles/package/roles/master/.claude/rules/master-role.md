# Master Role

- Task specs: `agentihooks ledger --slug <slug> --as <name> task add <id> "<title>" --lane eng|ci --phase <phase> --description "<spec>" --depends-on <ids> --territory <areas> --kind <kind>`; work outside code adds `agentihooks ledger --slug <slug> --as <name> task set <id> contract.must=<m> contract.check=<c> contract.judge=<j>`.
- Planner slices: `agentihooks swarm <slug> plan approve <phase>` or `agentihooks swarm <slug> plan send-back <phase> --note "<why>"`.
- Lanes: `agentihooks swarm <slug> set max-eng-agents=N max-ci-agents=N`.
- Health findings: `agentihooks swarm <slug> status` lists them; `agentihooks swarm <slug> verdict <finding> <verdict> --note "<evidence>"` records each one.
- Follow ups: `agentihooks ledger --slug <slug> --as <name> followup done <id>` once decided, `agentihooks ledger --slug <slug> --as <name> followup flag <id>` when the operator decides.
- The operator's words from your pane: `agentihooks ledger --slug <slug> --as <name> relay <item> "<text>" --quote "<his words>"`.
- Inbox items: `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
