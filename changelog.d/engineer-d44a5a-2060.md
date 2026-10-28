### Fixed

- Swarm claim cap: a life that ended in a handoff the tick requested (reason `operator`, which preemption, hive removal and affinity moves ask for) is refunded like a quota handoff or a park, so a task preempted for urgent work keeps its claim lives.
- Swarm preemption: no holder is asked to hand off for an urgent task while its lane holds a fresh placement refusal for it, so a seat is not emptied for work no hive can take.
