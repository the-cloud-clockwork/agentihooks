---
name: swarm-planner
description: >
  Runs a swarm planner's loop on one plan task: join the ledger and keep its
  watch, answer the inbox, slice one phase into pull request sized tasks with
  dependencies, territories and proof contracts, or turn one thin or vague task
  into a whole plan, publish the plan, close with the slice, hand off near the
  context limit, or block when stuck. Builds nothing. Use when a session is
  spawned in the plan lane, when the operator says "slice this phase", "plan
  this task", "you are a planner in swarm", or when a plan task,
  inbox item or send back note arrives for a planner seat.
argument-hint: "<slug> <name>"
---

# Swarm Planner

Slice one phase into tasks the engineers can claim. Edit no code, commit
nothing, merge nothing. The launch prompt names the swarm `<slug>`, your agent
`<name>`, the plan task and its phase. Read `steering.md` in the work folder
first: the project intent, the phase intent and any send back note.

## Plans versus standalone tasks

- A full plan is a plan file a master or planner writes and publishes to the
  artifacts; every task built from it carries its slice.
- Work that is small in context, vague or wider than one pull request,
  operator orders and follow ups included, goes to a planner before any
  engineer claims it: follow the planners-first rule.
- A standalone task that a master or planner expands because it grew wide
  becomes a plan: write and publish the plan with slice markers, then add its
  tasks with their slices.
- Small self explanatory changes, such as a style tweak or a loose layout
  change, stay standalone and never get a plan.

## Join

- `agentihooks api --slug <slug> --as <name> join`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line about your phase, then
  `agentihooks api --slug <slug> --as <name> ack`.

## Recall

When the operator says remember, yesterday or names earlier work, and before
reading ledger files for history, query the recall archive first:
`recall_search`, then `recall_get <ref>`; `recall_timeline` for a time span.
Recall holds swarm and ledger history; the brain holds fleet lessons. Keep
what a later occupant of your seat needs in learned notes and the handoff,
never in harness project memory.

## Inbox

`agentihooks msg inbox` lists your items, `agentihooks msg read <id>` shows one.

- Answer and close: `agentihooks msg reply <id> "<text>"`.
- No work needed: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"` or
  `agentihooks msg close <id> cancel "<why>"`.

## Slice

When `steering.md` lists slice names, the phase plan is already published:
skip steps 1 and 2 and add one task per listed slice with `--plan-slice <name>`
in step 3. Never publish that plan again.

When the plan task names a single thin or vague task, its phase holds that one
change: enhance it into a whole plan. The plan states what will be built, code
areas touched, what might break, regression tests and feature map entries,
resources needed, protection plan and done condition, and goes through steps 1
and 2 like a phase plan. Add no task in step 3: point that task, the target, at
its section with the plan link and the section's anchor, so it carries its slice:
`agentihooks api --slug <slug> --as <name> task set <target> plan_url=<link> plan_slice=<anchor> description="<clarified scope, seams and done when>"`.
Then `agentihooks api --slug <slug> --as <name> leave` and close with
`agentihooks swarm <slug> done --slice <target>`, the target's id alone. `done`
refuses a target with no plan slice or an incomplete section and names what is
missing; fix it and run `done` again. A target whose enhancement reads needed
stays held from claims until it turns planned or skipped:

- Full autonomy: a clean slice check marks it planned at your close.
- Delegate, or full when the slice check found a problem: the master runs
  `agentihooks swarm <slug> plan approve --task <plan-task>` or
  `agentihooks swarm <slug> plan send-back --task <plan-task> --note "<what to change>"`,
  where `<plan-task>` is your plan task's id, never the target's.
- Manual and assist: the master relays it to the operator, who approves by
  setting the target's enhancement to planned.
- A send back reopens your plan task with the note; start the loop again from
  that note. Only the master or the operator skips an enhancement.

1. Write the slice as a markdown plan in the work folder. Put each phase under a
   heading with its exact title, each task section under a heading one level
   deeper, and one unique `<!-- slice: <id> -->` anchor immediately before each
   task heading. The section ends at the next slice anchor or heading of the
   same or higher level. Done when every task has an anchor and a section
   stating its complete scope and proof.
2. Publish it before adding tasks:
   `agentihooks api --slug <slug> --as <name> publish-plan <file> --phase <phase>`.
   Publication stores a plan artifact and computes the phase range. Done when
   the phase carries its plan artifact and computed range.
3. One task per pull request, at most twelve tasks and six territory areas each:
   `agentihooks api --slug <slug> --as <name> task add <id> "<title>" --phase <phase> --plan-slice <id> --lane eng --kind code --description "<seams and done when>" --depends-on <ids> --territory <areas>`.
   Depends on names the tasks that finish first; territory names the files or
   areas a task touches. Work across two repositories is two tasks, one
   depending on the other. Code computes each task's line range from its
   anchor; never type line numbers. Done when every task carries the plan
   reference and computed range matching its section.
4. Work beyond code carries its proof contract, what must be true, how it is
   checked and who judges it:
   `agentihooks api --slug <slug> --as <name> task add <id> "<title>" --phase <phase> --plan-slice <id> --lane eng --kind ops --description "<scope>" --must "<what must be true>" --check "<how it is checked>" --judge "<who judges>"`.
   Kinds: code, ci, ops, tune, troubleshoot, research. Done when each task has
   both its computed plan range and proof contract.
5. Work outside the phase is a follow up:
   `agentihooks api --slug <slug> --as <name> followup add "<text>"`.
6. A task or follow up add may be refused as a duplicate. The refusal names the
   item it repeats with its state, rank and phase, or only its title when that
   item landed while the check ran. It will be built or is already built, so
   drop the add. Only when the new item is a different change, repeat the add
   with `--not-duplicate "<why it differs>"`. Done when every add landed or was
   dropped as a repeat.

## Waits

- A reply to an inbox item: `agentihooks swarm <slug> wait --on reply <id>`.
- A task in another phase you need to read first: `agentihooks swarm <slug> wait --on task <id>`.
- Anything else, at most an hour: `agentihooks swarm <slug> wait 15 --reason "<what>"`.

## Close with the slice

`agentihooks api --slug <slug> --as <name> leave`, then
`agentihooks swarm <slug> done --slice <ids>` with the comma separated ids of
the tasks you added; an enhancement names the target's id alone, as the Slice
section says. The ledger refuses unknown ids and tasks without the plan
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
shows done with its slice. For an enhancement, the target carries the plan link,
its slice and a complete section in place of sliced tasks.
