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
| `agentihooks swarm <id> expert start <mission file> [--effort xhigh\|max]` | Saves the mission and opens a running lineage. Its id, `expert-<n>`, is also its seat label. |
| `agentihooks swarm <id> expert status [<lineage>] [--json]` | One line per running lineage, or the one named: state, effort, the expert on its seat while it runs, mission title, who started it and who ended it. |
| `agentihooks swarm <id> expert mission [<lineage>]` | Prints the mission line and the saved text. An expert reads its own mission and no other. |
| `agentihooks swarm <id> expert stop <lineage> --note "<why>"` | Ends a running lineage by order. The note is kept. |
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

A refused proof changes nothing: the lineage keeps running and the expert does
the open work. An accepted proof ends the lineage as `complete`, saves the
proof, writes the seat recap, finishes the expert and tells the master.

## The Stop gate

While its lineage runs, the end of an expert's turn is refused by the
`expert-stop` gate, unless the expert handed its seat off or holds a declared
wait. The refusal names the ways on: keep working, hand off, complete on a
proof, or declare the wait. The gate ships at `deny`; its mode is set like
every gate with `agentihooks swarm <id> set expert-stop-gate=...`.

## Checks

Each part has a live check the Doctor runs on its timer:

| Check | Passes when |
|---|---|
| `python -m scripts.feature_checks.expert_launch_pin` | Every live expert runs Claude Code on Fable at its lineage's effort. |
| `python -m scripts.feature_checks.expert_missions` | Every running lineage's saved mission matches its address. |
| `python -m scripts.feature_checks.expert_handoffs` | Every handoff a running lineage holds conforms to the expert handoff. |
| `python -m scripts.feature_checks.expert_claims` | No ledger task is claimed by an expert. |
| `python -m scripts.feature_checks.expert_proofs` | Every completed lineage holds a proof that conforms. |
| `python -m scripts.feature_checks.expert_stop_gate` | Every running swarm keeps the Stop gate at `deny`. |
| `python -m scripts.feature_checks.expert_role` | The shipped expert role holds its persona, rule, both skills, Stop gate and identity pin. |

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `mission refused` on start | The mission breaks the template | Fix each fault the command lists and start again. |
| `mission proof refused` on complete | Work is still open or not shown | Do the work each fault names, then submit again. |
| An expert's turn does not end | Its lineage runs and it holds no wait | It is working as designed. Stop the lineage with `expert stop` to end it by order. |
| `<name> is an expert; an expert works its mission and claims no task` | An expert ran a worker verb | Do the work inside the mission, or ask the master. |
