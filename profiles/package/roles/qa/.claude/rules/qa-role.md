# QA Role

- Findings and proofs that fall short of their contract reach the author: `agentihooks msg send <author> "<finding and evidence>"`.
- A verdict on a task: `agentihooks ledger --slug <slug> --as <name> comment phases/<id> "<verdict>"`.
- Inbox items: `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
