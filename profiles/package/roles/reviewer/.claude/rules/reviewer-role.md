# Reviewer Role

- Findings reach the author too: `agentihooks msg send <author> "<findings and the review link>"`.
- A review verdict on the task's phase: `agentihooks api --slug <slug> --as <name> comment phases/<id> "<verdict>"`.
- Inbox items: `agentihooks msg close <id> done|handoff <address>|blocked <what>|cancel`.
