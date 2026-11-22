---
title: Expert lineage
parent: Reference
nav_order: 96
---

# Expert lineage

An expert is a swarm agent built for one mission. It always runs Claude Code
on the Fable model, at effort `xhigh` unless the lineage was started at `max`.
It holds a seat, hands off at the context limit and keeps seat notes like
every swarm agent, and it claims no ledger task.

Experts that follow one another on the same seat form a lineage. Each one
works the same saved mission, hands the seat to the next near its context
limit, and the lineage ends only when the swarm accepts a mission proof. Use
one for the hardest work a swarm has: a problem that needs one mind on it from
the first reading of the evidence to the deployed proof.

## The mission

The mission is a document saved by the swarm, never text that lives only in a
prompt. Every expert of the lineage reads the same words.

```bash
agentihooks swarm <id> expert template > mission.md
```

The template holds the line `# Mission: <title>` and five sections, each once
and in this order:

| Section | Holds |
|---|---|
| The operator's words | The order as the operator wrote it, word for word. They win over any reading of them. |
| The problem | What is wrong or missing, in plain words. |
| Evidence | What is proven, with its source, and what is not proven yet. |
| What to deliver | The deliverables, numbered from 1 with no gap. |
| How to report | Who hears what, and at which points. |

`expert start` refuses a mission that breaks the template and names each
fault. The saved text is addressed as `mission:<swarm>/<sha256>`; the same
text has the same address on any lineage.

## Start and steer a lineage

Starting and stopping belong to the master of the swarm and the operator.

| Command | What it does |
|---|---|
| `agentihooks swarm <id> expert start <mission file> [--effort xhigh\|max] [--handoff <doc>] [--from <session>]` | Saves the mission and opens a running lineage. Its id, `expert-<n>`, is also its seat label. The swarm launches its first expert on the next pass. With `--handoff`, the first expert continues a session that was not an expert: see Starting from a handoff. |
| `agentihooks swarm <id> expert status [<lineage>] [--json]` | One line per running lineage, or the one named: state, effort, the expert on its seat while it runs, mission title, who started it and who ended it. |
| `agentihooks swarm <id> expert mission [<lineage>]` | Prints the mission line and the saved text. An expert reads its own mission and no other. |
| `agentihooks swarm <id> expert stop <lineage> --note "<why>"` | Ends a running lineage by order. The note is kept, and a handoff that waited on its seat is cleared. |
| `agentihooks swarm <id> expert proof <lineage>` | Prints the mission proof a completed lineage ended on. |

## What an expert may do

An expert does the work inside its mission. The swarm command refuses it every
worker verb (`issue`, `pr`, `done`, `block`, `park`, `ready` and the others),
and the ledger refuses it a task add, any task change, a follow up, a task
group and a split. It still queues and dequeues its own pull requests with
`agentihooks swarm <id> merge queue|dequeue <url>`, asks the master through the
inbox and declares its waits with `agentihooks swarm <id> wait`.

The expert role profile carries two skills: `swarm-expert`, the loop from
reading the mission to the mission proof, and `expert-handoff`.

## Handing off

Near its context limit an expert writes an expert handoff and submits it with
the same command every agent uses:

```bash
agentihooks swarm <id> handoff <doc>
```

The body is wider than Handoff v2. It opens with the mission line, names every
deliverable once as done or pending, and carries the assumptions, the tests
and the validation, so the next expert continues exactly where this one
stopped. The next expert confirms the first Next action with
`agentihooks swarm <id> confirm-handoff <transfer> --next "<action>"`.

## Ending on a mission proof

No agent's judgment ends a lineage. The expert that works it submits a mission
proof and code checks it:

```bash
agentihooks swarm <id> expert complete <proof file>
```

| The proof must | Otherwise |
|---|---|
| Open with the mission line | The command names the line it expects. |
| Name every deliverable once, by a bullet that starts `D` and its number | Each missing, doubled or unknown number is named. |
| Hold at least one bullet under Validation, Smoke tests, Regression tests, Semgrep rules and Feature map | The empty section is named. |
| Carry evidence on every one of those bullets, with none labelled hypothesis | The bullet is named. |
| Say `None` under Open items | The proof is refused while anything is open. |
| Link only merged pull requests under Deliverables | Each unmerged one is named. |
| Write each pull request under Deliverables as its full link | A `#number`, an issue link or a link in another form is named. |

A refused proof changes nothing: the lineage keeps running and the expert does
the open work. An accepted proof ends the lineage as `complete`, saves the
proof, tells the master, writes the seat recap and finishes the expert. If the
command fails after it accepted the proof, running it again with the same proof
finishes those steps; the master is told once.

## The Stop gate

While its lineage runs, the end of an expert's turn is refused by the
`expert-stop` gate, unless the expert handed its seat off or holds a declared
wait. The refusal names the ways on: keep working, hand off, complete on a
proof, or declare the wait. An expert whose lineage ended on a proof but whose
completion did not finish is refused too and told to run the completion again.
Its default mode is `deny`. It is set like every gate, to one of three modes:
`agentihooks swarm <id> set expert-stop-gate=deny`,
`agentihooks swarm <id> set expert-stop-gate="log only"` (quoted, the name holds a space) or
`agentihooks swarm <id> set expert-stop-gate=skip`.

## How a seat is kept

Nobody launches an expert by hand. Every pass of the swarm tick looks at each
running lineage and keeps its seat:

| The pass finds | It does |
|---|---|
| A running lineage with an empty seat | Launches one expert on it. The expert's opening names its number in the lineage. |
| A seat that holds an expert, working or not yet retired | Nothing: one seat never holds two experts. |
| An accepted handoff waiting on the seat | Launches the next expert with that handoff and the transfer it confirms. |
| An empty seat whose last expert was lost | Leaves the next expert a handoff first. If the lost expert never confirmed the handoff it was given, that handoff is offered again. Otherwise the swarm writes a recovery handoff: every deliverable pending and marked unknown, the mission and the seat recap to read first. |
| An expert whose lineage was stopped | Closes its open mail with the reason, clears the handoff left on the seat and finishes it. |
| A completed lineage whose closing steps are still owed | Runs them from the kept proof, with or without its expert's record: the master is told once, the recap is kept and the handoff left on the seat is cleared, even when the expert's session died right after its proof was accepted and the swarm already dropped it. |
| An expert of a lineage the swarm does not hold | Finishes it and goes on with the other seats. |
| A lineage whose part of the pass is refused by the swarm store or the inbox | Says so in one line that names the lineage and goes on with the others; a completed lineage keeps owing its closing steps until they finish. |

A launch waits, and says why, while the swarm has no session slot, the host is
full or the lineage is placed elsewhere. Nothing launches while the swarm is
paused, stopping, stopped or in panic. A sleeping swarm still keeps its expert
seats: a lineage waits for no ledger work. The handoff an expert was given stays on
the seat until that expert confirms it.

## Starting from a handoff

A session that was not an expert can pass its work to a lineage. It writes a
Handoff v2, and the master or the operator starts the lineage from it:

```bash
agentihooks swarm <id> expert start <mission file> --handoff <doc> --from <session>
```

The document must pass the Handoff v2 check, or nothing starts. The first
expert gets it in its opening with a transfer to confirm, like any successor.
`--from` names the session the document comes from; without it the starter is
recorded as its author.

## Checks

Each part has a live check the Doctor runs on its timer:

| Check | Passes when |
|---|---|
| `python -m scripts.feature_checks.expert_launch_pin` | Every live expert runs Claude Code on Fable at its lineage's effort. |
| `python -m scripts.feature_checks.expert_missions` | Every running lineage's saved mission matches its address. |
| `python -m scripts.feature_checks.expert_handoffs` | Every handoff a running lineage holds conforms to the expert handoff, and the one a lineage was started from to Handoff v2. |
| `python -m scripts.feature_checks.expert_seats` | No lineage seat is held by two experts, and every expert on a seat belongs to a lineage the swarm started. |
| `python -m scripts.feature_checks.expert_claims` | No ledger task is claimed by an expert. |
| `python -m scripts.feature_checks.expert_proofs` | Every completed lineage holds a proof that conforms. |
| `python -m scripts.feature_checks.expert_stop_gate` | Every running swarm keeps the Stop gate at `deny`. |
| `python -m scripts.feature_checks.expert_role` | The shipped expert role holds its persona, rule, both skills, Stop gate, identity pin and prompt guard. |

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `mission refused` on start | The mission breaks the template | Fix each fault the command lists and start again. |
| `mission proof refused` on complete | Work is still open or not shown | Do the work each fault names, then submit again. |
| An expert's turn does not end | Its lineage runs and it holds no wait | It is working as designed. Stop the lineage with `expert stop` to end it by order. |
| `expert status` shows `no expert yet` | The swarm is paused, or the launch waits on a session slot, the host or a hive | Read the tick's line for the lineage; it names what it waits on. |
| `handoff refused` on start | The document given with `--handoff` breaks Handoff v2 | Fix each fault the command lists and start again. |
| `<name> is an expert; an expert works its mission and claims no task` | An expert ran a worker verb | Do the work inside the mission, or ask the master. |
