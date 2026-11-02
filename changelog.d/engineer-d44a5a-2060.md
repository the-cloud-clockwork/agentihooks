### Fixed

- Swarm claim cap: a holder the tick asked to hand off for an urgent task gets its claim life refunded when it hands off with reason `operator`, like a quota handoff or a park, so a task preempted for urgent work keeps its claim lives.
- Swarm preemption: no holder is asked to hand off for an urgent task whose last placement attempt in its lane was refused within the last thirty minutes, so a seat is not emptied for work no hive can take.
