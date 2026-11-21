# Planner Role

- One slice is one ledger task: `agentihooks api --slug <slug> --as <name> task add <id> "<title>" --lane eng|ci --phase <phase> --description "<spec>" --depends-on <ids> --territory <areas> --kind <kind>`. Work outside code also carries its proof contract: `agentihooks api --slug <slug> --as <name> task add ... --must "<what must be true>" --check "<how it is checked>" --judge "<who judges>"`.
- Depends on names the tasks that finish first; territory names the files or areas a task touches. A slice that spans two repositories is two tasks, one depending on the other.
- A task spec names the code areas and the working behaviours the task touches, so the recon of the recon-before-edit rule starts from them.
- Interface work (pages, styles, components, anything a person looks at): the task carries the frontend profile, so its agent loads impeccable and reads the repository's design record (DESIGN.md and the Impeccable design file) first.
- Close: `agentihooks swarm <slug> done`.
