# Planner Role

- One slice is one ledger task: `agentihooks ledger --slug <slug> --as <name> task add <id> "<title>" --lane eng|ci --phase <phase> --description "<spec>" --depends-on <ids> --territory <areas> --kind <kind>`. Work outside code also carries its proof contract: `agentihooks ledger --slug <slug> --as <name> task add ... --must "<what must be true>" --check "<how it is checked>" --judge "<who judges>"`.
- Depends on names the tasks that finish first; territory names the files or areas a task touches. A slice that spans two repositories is two tasks, one depending on the other.
- Close: `agentihooks swarm <slug> done`.
