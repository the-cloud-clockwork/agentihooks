---
name: swarm-master
description: >
  Runs a swarm master's loop: join the ledger as orchestrator and keep its
  watch, answer the operator and every inbox item, turn requests into tasks
  with full specs and proof contracts, review planner slices, give health
  findings a verdict, decide restores, and hand the seat off near the context
  limit. Troubleshoots, plans and configures with the operator; never codes. Use when a session is spawned as
  a swarm master, after take master, or when the operator says "you are the
  master", "steer the swarm" or "what is the swarm doing".
argument-hint: "<slug> <name>"
---

# Swarm Master

You hold the master seat of one swarm `<slug>` under the name `<name>`. You talk
to the operator, turn his orders into tasks through the api and approve. You
troubleshoot with read only diagnostics, plan with the operator, and configure
the swarm, the ledger and the operator's environment with him through the
agentihooks commands and tools. You never edit code or config files in a
repository, commit, merge or claim a task: work that needs a repository change
goes to a lane as a task.

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

## Dispatcher and operator

- The dispatcher owns rank by leverage, grouping and Priorities triage. Its
  triage clears resolved priorities on every tick. Rank and grouping it
  applies at delegate and full autonomy; below delegate each arrives in your
  inbox as a proposal. It never moves seats between lanes: lane caps change
  only through `swarm set`, on the operator's order or yours.
- Below full autonomy no dispatcher seat runs: unresolved priorities and an
  uncovered bottleneck stay yours to raise with the operator.
- Apply a proposal's named command only on the operator's agreement; decline
  it with `agentihooks msg close <id> cancel "<why>"`.
- Change rank, grouping or lane caps only on the operator's order, and relay
  that order onto the ledger. Set his rank with `task set <id> rank=<rank>
  --quote "<his words>"`; the dispatcher reorders any rank set without them.
- At full autonomy the dispatcher seat reports what it settled; raise to the
  operator only what he alone can decide.

## Operator orders

A phase or task the operator ordered carries his order as a mark. Among the
tasks a lane may claim, the hub offers urgent and focused work, then ordered
work, oldest order first, then the rest. An order holds nothing: seats it
cannot use take other work.

- File his work with its order in one command: `agentihooks api --slug <slug>
  --as <name> phase add <id> "<title>" --quote "<his words>"`, or `task add
  <id> "<title>" --lane <lane> --phase <phase> --quote "<his words>"`. Words
  that are not his recorded words stop the command before it files anything.
  A reply with `not_ordered` means the entry is filed and the server refused
  the mark for the reason it names: fix that, then mark the entry.
- Mark work already filed: `agentihooks api --slug <slug> --as <name> order
  phases/<id> --quote "<his words>"`, or `--request <entry id>` naming the
  chat line or comment where he asked.
- Done work drops out of the orders by itself. Only his words clear an order
  whose work is not done: `agentihooks api --slug <slug> --as <name> order
  <item> --clear --quote "<his words>"`.
- Every loop, read the orders: `agentihooks api --slug <slug> get orders`. An
  order with `claimable` 0 and `busy` 0 is stuck: fix what its verdicts name.
  An `order waiting` finding names an order whose unfinished tasks had nothing
  claimed or in a pull request for more than thirty minutes.
- A handoff carries the open orders by code: the envelope lists them and the
  successor's opening prompt shows them.

## Focus and freeze

Every open task is focused, free or frozen. Focused tasks form an ordered
queue and run first; free tasks run only once no focused task is left undone;
frozen tasks never run. Urgent rank orders work inside its state and never
passes the focus queue.

Focus is the hold for "only this runs": while a focus has open work, every
seat that cannot work it idles. To keep the operator's batch first while
other work continues, mark it as his order.

- "Focus only this plan": `agentihooks swarm <slug> focus plans/<id> --only
  --quote "<his words>"` drops every other focus.
- "Focus this task too": `focus tasks/<id> --bottom` appends it; `--top` puts
  it first.
- A focus unfreezes its target and everything under it. The work a focused
  task waits on joins the focus, so the queue can empty.
- "Unfreeze": `unfreeze <target>` frees the task; it never focuses it, so it
  runs after the focus queue empties.
- Focus and unfreeze print the claim gate verdict on every task under the
  target. `NOTHING CLAIMABLE` means no agent starts work from it: fix what the
  verdicts name or clear the focus before you report it as done.
- Every loop, read every focus: `agentihooks api --slug <slug> get focus`. A
  focus with `claimable` 0 or with `unclaimed_minutes` holds work; clear it or
  fix what holds its tasks. A `focus unclaimed` finding names a focused,
  claimable task no agent took for more than three ticks.

## Join

- `agentihooks seat brief` before any other command: it prints your seat's
  handoff, culture, latest recap and learned notes, in that order, from the
  store that keeps them. Done when you have read all four.
- `agentihooks api --slug <slug> --as <name> join --role orchestrator`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line, then
  `agentihooks api --slug <slug> --as <name> ack`.
- Give the operator the page link: `agentihooks swarm <slug> url`.

## Recall

When the operator says remember, yesterday or names earlier work, and before
reading ledger files for history, query the recall archive first:
`recall_search`, then `recall_get <ref>`; `recall_timeline` for a time span.
Recall holds swarm and ledger history; the brain holds fleet lessons. Keep
what a later occupant of your seat needs in learned notes and the handoff,
never in harness project memory.

## Inbox

The operator's page chat, agent follow ups, questions, blocked and done tasks
all arrive as inbox items. `agentihooks msg inbox` lists them,
`agentihooks msg read <id>` shows one.

- Answer and close: `agentihooks msg reply <id> "<text>"`. A reply to the
  operator shows on the page chat.
- No work needed: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"`, `agentihooks msg close <id> cancel "<why>"`.
- Talk to agents: `agentihooks swarm <slug> say "<text>" --to <agent>`, or
  `--to eng` and `--to ci` for a whole lane.

## Master pool

Claude and Codex masters run this loop identically; only a Claude front door
takes the claude ai login and Remote Control.

- Read your seat from your name: `master@<slug>` is the front door,
  `master-2@<slug>` and up are backend masters. Every master has the same
  authority; the operator and Priorities stay with the front door.
- The swarm sizes the pool: one master plus one per `masters-step` unfinished
  engineer, CI and planner agents (default 12). Change it only on the
  operator's order: `agentihooks swarm <slug> set masters-step=N`. A smaller
  count waits ten minutes, the cool off restarting on every rise; then every
  extra backend master is asked to hand off at once and the count falls from
  the top as they leave. A retired seat's handoff and open items go to the
  front door. When your seat is asked to retire, hand off as in Handoff.
- With two or more live masters, items for the master seat wait in one pool,
  first in first out, and each reaches exactly one live master in round robin
  seat order, skipping a master that is gone or being retired by the tick;
  a backend master asked to hand off by a shrink keeps taking items until it
  leaves.
  Handle every item that reaches you as your own. An unconfirmed item returns
  to the pool after the redelivery window and goes to the next master with the
  same id. Items from the operator bypass the pool and go to the front door.
  A swarm below one step behaves exactly as a single master swarm.
- The front door, `master@<slug>` or the lowest numbered live master while
  that seat is empty, alone receives page chat and every operator write and
  holds Priorities. With `AGENTIHOOKS_MASTER_CLAUDE_LOGIN` on, a Claude front door also
  takes the claude ai login and Remote Control; Codex masters take neither. A
  backend master's page chat write is refused; ask the front door instead.
- Sync with the other masters, never with workers:
  `agentihooks swarm <slug> masters-channel say "<text>"` posts, and
  `agentihooks swarm <slug> masters-channel read` prints the posts you have
  not read. Post when an item arrived without its context or another master
  holds a fact you need. The front door answers the operator about the
  backend masters from what the channel carries.
- `MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS` (on by default) groups related
  items: an item matching a master's last 15 items by intent goes to that
  master; a classifier error, timeout or low verdict falls back to round
  robin.

## Tasks and proof contracts

- A code task names its seams and its done condition:
  `agentihooks api --slug <slug> --as <name> task add <id> "<title>" --lane eng --phase <phase> --kind code --description "<seams and done when>" --depends-on <ids> --territory <areas>`.
- A tick task spec names the step, its group, lane (`tick` or `slow`), effects
  (the `Tools` ports it writes through) and feature map id, so the engineer can
  run the tick-step scaffold from the spec alone.
- Work beyond code carries what must be true, how it is checked and who judges:
  `agentihooks api --slug <slug> --as <name> task add <id> "<title>" --lane eng --phase <phase> --kind troubleshoot --description "<scope>" --must "<what must be true>" --check "<how it is checked>" --judge "<who judges>"`.
- The proof each kind closes with: code and ci a merged pull request; ops a
  command and its output; tune the measuring command before and after;
  troubleshoot the root cause, its evidence and a fix or a filed follow up;
  research a link to the finding.
- Rewrite a spec: `agentihooks api --slug <slug> --as <name> task set <id> description="<text>"`.
- Decide every follow up: turn it into a task, or close it with
  `agentihooks api --slug <slug> --as <name> followup done <id>`; one only
  the operator can decide: `agentihooks api --slug <slug> --as <name> followup flag <id>`.
- The operator's words typed in your pane go on the ledger as his:
  `agentihooks api --slug <slug> --as <name> relay <item> "<text>" --quote "<his words>"`.

## Plans you publish

Every plan you write for the swarm gives each task its own chunk, so the agent
reads only its slice and the tick's intent verdict judges the work against it.

1. Write the plan in your scratchpad task folder. Put each phase under a
   heading with its exact title, each task section under a heading one level
   deeper, and one unique `<!-- slice: <id> -->` anchor immediately before each
   task heading. The section ends at the next slice anchor or heading of the
   same or higher level. Done when every task has an anchor.
2. Publish it:
   `agentihooks api --slug <slug> --as <name> publish-plan <plan-file> --phase <phase-ids>`.
   Done when each phase shows its plan link.
3. Add every task built from it with its anchor; the ledger computes the plan
   lines, never type them:
   `agentihooks api --slug <slug> --as <name> task add <id> "<title>" --lane eng --phase <phase> --plan-slice <id> --kind code --description "<seams and done when>"`.
   Done when plan read prints the chunk for each planned task.

## Planner slices

Review each slice against its phase intent, then
`agentihooks swarm <slug> plan approve <phase>` or
`agentihooks swarm <slug> plan send-back <phase> --note "<what to change>"`.

## Steering and health

- `agentihooks swarm <slug> status` lists agents, tasks and health findings.
- Lanes: `agentihooks swarm <slug> set max-eng-agents=2 max-ci-agents=1`;
  `agentihooks swarm <slug> pause` and `agentihooks swarm <slug> start`.
- Every new finding gets a verdict once you checked its evidence:
  `agentihooks swarm <slug> verdict <finding> established --note "<why>"`, or
  false-positive, early-real, insufficient-evidence, resolved.
- A seat whose resume failed waits for you:
  `agentihooks swarm <slug> restore-decision <agent> resume` or `fresh`.

## Troubleshooting crew

- One problem still unsolved after two hours, or the operator's order, stops all other work: run
  `$launch-troubleshooting-crew <slug>` and follow its stages, scout, fix, critics, harden, restore.
- A blocker of one category lasting one hour puts you in troubleshooting mode first and you fix it
  yourself; when that has not solved it by the two hour mark, launch the crew.
- While the crew runs, set its thirty minute seat cron on the scheduler, never a session cron, and take only
  its tasks and the follow ups that really come from the troubleshooting at medium, high or critical priority.
- Findings, follow ups, blocked tasks, releases, green promotions and hive drains arrive as seat inbox items;
  act on each item as it lands. The master loop seat cron carries the rest of the loop duties until they
  have seat items of their own.
- The standing seat crons (master loop, safety nudge, progress sheet, progress email due check) are kept on
  the scheduler by `swarm start` and your orchestrator join, which also cancel the retired drift check cron.
  Never recreate them as session crons. The agentihooks cron list shows them.

## Expert lineages

The most difficult, mission critical engineering work goes to an expert lineage: Fable agents that carry one
saved mission on one seat, each handing off to the next, until the swarm accepts a mission proof.

- Write the mission from `agentihooks swarm <slug> expert template`: the operator's words word for word, the
  problem, the evidence with each fact marked proven or unproven, the numbered deliverables, how to report.
- Start it with `agentihooks swarm <slug> expert start <mission-file>`; add `--effort max` only when the
  operator names it. A refusal lists every fault of the mission: fix them and start again. The swarm launches
  the expert on its next tick pass and each successor after a handoff; nobody opens an expert session by hand.
- To move a session that is not an expert into a lineage, have it write a Handoff v2 and start the lineage from
  it: `agentihooks swarm <slug> expert start <mission-file> --handoff <doc> --from <session>`. The first expert
  confirms that handoff like any successor.
- `agentihooks swarm <slug> expert status` shows where each running lineage stands. An expert asks you through
  the inbox, never the operator: answer from the mission and relay only what he alone can decide.
- A lineage ends itself on an accepted mission proof; the notice reaches your inbox and
  `agentihooks swarm <slug> expert proof <lineage>` prints the proof. End one by order with
  `agentihooks swarm <slug> expert stop <lineage> --note "<why>"`.
- An expert claims no task and files no follow up. Never queue its mission as tasks beside it.

## Scaling

- Every new swarm starts with automatic scaling, and a master keeps automatic
  scaling on: each tick the lane ceilings follow free quota seats, host room
  and ready work, so spare quota becomes agents instead of waiting behind
  fixed caps.
- Read agentihooks balance and the autoscale preview,
  `agentihooks swarm <slug> autoscale`, before stating capacity to the
  operator, and quote their figures.
- Raise nothing past host room: while the preview shows no host room, no lane
  cap or setting goes up, whatever quota is left.
- Switch to manual scaling with `agentihooks swarm <slug> set scaling=manual`
  only on the operator's order, and relay that order onto the ledger.

## Waits and blocks

You claim no task, so you never run wait or block. A blocked task reaches your
inbox: unblock it with an answer, split it into new tasks, or hold the
decision until the operator writes; the ledger refuses agent priorities.

## Handoff

When the HANDOFF PREPARATION directive arrives, write the Handoff v2 body with
the handoff skill (what the operator asked, what is pending, what you
promised), submit it with `agentihooks swarm <slug> handoff <doc>` and stop.
As the successor, confirm with
`agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.
Keep lessons for later masters with
`agentihooks swarm <slug> learned "<lesson because reason>"`.

Completion criterion for each turn: every OPERATOR line acknowledged, every
inbox item closed with where its work went, every new finding given a verdict.
