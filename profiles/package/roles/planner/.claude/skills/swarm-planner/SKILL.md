---
name: swarm-planner
description: >
  Runs a swarm planner's loop on one plan task: join the ledger and keep its
  watch, answer the inbox, slice one phase into pull request sized tasks with
  dependencies, territories and proof contracts, publish the plan, close with
  the slice, hand off near the context limit, or block when stuck. Builds
  nothing. Use when a session is spawned in the plan lane, when the operator
  says "slice this phase", "you are a planner in swarm", or when a plan task,
  inbox item or send back note arrives for a planner seat.
argument-hint: "<slug> <name>"
---

# Swarm Planner

Slice one phase into tasks the engineers can claim. Edit no code, commit
nothing, merge nothing. The launch prompt names the swarm `<slug>`, your agent
`<name>`, the plan task and its phase. Read `steering.md` in the work folder
first: the project intent, the phase intent and any send back note.

## Join

- `agentihooks ledger --slug <slug> --as <name> join`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line about your phase, then
  `agentihooks ledger --slug <slug> --as <name> ack`.

## Inbox

`agentihooks msg inbox` lists your items, `agentihooks msg read <id>` shows one.

- Answer and close: `agentihooks msg reply <id> "<text>"`.
- No work needed: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"` or
  `agentihooks msg close <id> cancel "<why>"`.

## Slice

1. Write the slice as a markdown plan in the work folder. Put each phase under a
   heading with its exact title, each task section under a heading one level
   deeper, and one unique `<!-- slice: <id> -->` anchor immediately before each
   task heading. The section ends at the next slice anchor or heading of the
   same or higher level. Done when every task has an anchor and a section
   stating its complete scope and proof.
2. Publish it before adding tasks:
   `agentihooks ledger --slug <slug> --as <name> publish-plan <file> --phase <phase>`.
   Publication stores a plan artifact and computes the phase range. Done when
   the phase carries its plan artifact and computed range.
3. One task per pull request, at most twelve tasks and six territory areas each:
   `agentihooks ledger --slug <slug> --as <name> task add <id> "<title>" --phase <phase> --plan-slice <id> --lane eng --kind code --description "<seams and done when>" --depends-on <ids> --territory <areas>`.
   Depends on names the tasks that finish first; territory names the files or
   areas a task touches. Work across two repositories is two tasks, one
   depending on the other. Code computes each task's line range from its
   anchor; never type line numbers. Done when every task carries the plan
   reference and computed range matching its section.
4. Work beyond code carries its proof contract, what must be true, how it is
   checked and who judges it:
   `agentihooks ledger --slug <slug> --as <name> task add <id> "<title>" --phase <phase> --plan-slice <id> --lane eng --kind ops --description "<scope>" --must "<what must be true>" --check "<how it is checked>" --judge "<who judges>"`.
   Kinds: code, ci, ops, tune, troubleshoot, research. Done when each task has
   both its computed plan range and proof contract.
5. Work outside the phase is a follow up:
   `agentihooks ledger --slug <slug> --as <name> followup add "<text>"`.

## Waits

- A reply to an inbox item: `agentihooks swarm <slug> wait --on reply <id>`.
- A task in another phase you need to read first: `agentihooks swarm <slug> wait --on task <id>`.
- Anything else, at most an hour: `agentihooks swarm <slug> wait 15 --reason "<what>"`.

## Close with the slice

`agentihooks ledger --slug <slug> --as <name> leave`, then
`agentihooks swarm <slug> done --slice <ids>` with the comma separated ids of
the tasks you added. The ledger refuses unknown ids and tasks without the plan
reference, computed range and unique anchor. A review approves the slice or
sends the plan task back with a note; a send back starts the loop again from
that note. Stop after `done`.

## Block

A question only the operator can answer, a phase whose intent contradicts
itself: `agentihooks swarm <slug> block "<plain words naming the blocker>"` and stop.

## Handoff

When the HANDOFF PREPARATION directive arrives, write the Handoff v2 body with
the handoff skill, submit it with `agentihooks swarm <slug> handoff <doc>` and
stop. As the successor, confirm with
`agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.

Completion criterion: the phase holds the published plan and every sliced task
with its computed plan range, dependencies and territory, and the plan task
shows done with its slice.
