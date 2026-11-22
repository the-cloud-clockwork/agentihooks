# Expert Role

- Mission: `agentihooks swarm <slug> expert mission` prints the mission line and the saved text. Read it first in every session.
- Successor: confirm the handoff you inherit with `agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.
- Waits: `agentihooks swarm <slug> wait --on checks <url>`, `agentihooks swarm <slug> wait --on merge <url>`, `agentihooks swarm <slug> wait --on reply <id>`, or `agentihooks swarm <slug> wait <minutes> --reason "<what>"`. A stop with no wait is refused while your lineage runs.
- Pull requests: queue one on green checks with `agentihooks swarm <slug> merge queue <url>` and read its place with `agentihooks swarm <slug> merge state <url>`.
- Master: `agentihooks msg send master@<slug> "<plain words>"`. Inbox items: `agentihooks msg reply <id> "<text>"` or `agentihooks msg close <id> done|blocked <what>|cancel`.
- Lessons for the next expert of your seat: `agentihooks swarm <slug> learned "<lesson because reason>"`.
- Handoff near the context limit: `agentihooks swarm <slug> handoff <doc>` with the expert handoff, then stop.
- End: `agentihooks swarm <slug> expert complete <proof>` with the mission proof. A refusal lists every fault: do the open work and run it again.
