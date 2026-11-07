---
priority: 1
description: Before any tool call made to find missing context, a swarm agent reads its seat's learned notes first, then the brain
alwaysApply: true
---

# Seat Learnings First (HARD RULE)

Operator's order, 1 November.

- The swarm is a distributed system. What an earlier occupant of a seat learned lives in that seat's learned notes, not in the current context window.
- When the context does not hold what the operator or the task asks for (a tool, an account, a document, a past decision, how to reach something), read the seat's learned notes before any other tool call: `agentihooks swarm <slug> learned`, searching the notes for the subject.
- Act on a matching note, canon first. Only when no note matches, go on to the brain, then to discovery with other tools.
- Never reach for a browser, a guess or a new probe for something a learned note already answers.
- A discovery that a later occupant will need goes back to the seat with `agentihooks swarm <slug> learned "<lesson because reason>"`.
