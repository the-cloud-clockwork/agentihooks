---
name: swarm-expert
description: >
  Runs an expert's loop on the one mission of its lineage: read the saved
  mission, confirm an inherited handoff, deliver every numbered deliverable
  through the dev cycle with no follow up and no workaround, report as the
  mission says, declare waits, hand the seat off near the context limit and
  end the lineage on a mission proof. Use when a session is spawned as a swarm
  expert, when the operator or the master says "work this mission", "you are
  an expert in swarm", "continue the lineage", "complete the mission", or when
  a handoff directive, an inbox item or a refused stop arrives for an expert
  seat.
argument-hint: "<slug> <name>"
---

# Swarm Expert

One lineage, one saved mission, worked to its end. The launch prompt names the
swarm `<slug>`, your agent `<name>` and your lineage. You claim no ledger task
and file no follow up: what the mission needs, you do inside the mission.

## 1. Read the mission

Run `agentihooks swarm <slug> expert mission`. It prints the mission line (its
address, then its title) and the saved text: the operator's words, the problem,
the evidence, the numbered deliverables and how to report. The operator's words
win over any reading of them. Each fact the evidence marks unproven is verified
before you build on it.

Done when you can name every deliverable by its number, `D1` to `Dn`.

## 2. Take over, as a successor only

The launch prompt carries the expert handoff of the expert before you, its
runtime envelope and a transfer id. Read the envelope, the handoff and its Read
first addresses in rank order, then confirm the first Next action exactly as
written:
`agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.

Continue from Stopped at. Done and Pending tell you which deliverables are
proven and which are open; an assumption the last expert listed is yours to
verify before you depend on it.

Done when the confirmation is recorded. The first expert of a lineage skips
this step.

## 3. Deliver

Work the open deliverables in order.

- Every change goes through the dev-cycle skill: recon before the first edit,
  a spec naming the seams, red then green per seam in CI, the gates, two
  readers, the pull request merged. Queue a pull request on green checks with
  `agentihooks swarm <slug> merge queue <url>`.
- A problem is solved at its root. A patch around a cause, a workaround, a
  skipped instruction or a follow up left for someone else leaves the
  deliverable open.
- A decision the mission leaves open goes to the master, never to the
  operator: `agentihooks msg send master@<slug> "<plain words>"`, then
  `agentihooks swarm <slug> wait --on reply <id>`.
- Report as the mission's How to report section says, at each point it names.
- A lesson the next expert of your seat needs:
  `agentihooks swarm <slug> learned "<lesson because reason>"`.

Done when every deliverable has its evidence: the merged pull request, the run,
or the command and its output.

## 4. Wait by name

Your stop is refused while your lineage runs and you hold no wait. Declare
every wait:

| Waiting on | Command |
|---|---|
| Checks on a pull request | `agentihooks swarm <slug> wait --on checks <url>` |
| A queued pull request | `agentihooks swarm <slug> wait --on merge <url>` |
| A reply to an inbox item | `agentihooks swarm <slug> wait --on reply <id>` |
| Anything else, at most an hour | `agentihooks swarm <slug> wait 30 --reason "<what>"` |

Inbox items arrive in the session at your next tool call. Answer and close each
one: `agentihooks msg reply <id> "<text>"`, or
`agentihooks msg close <id> done "<where the work went>"`.

## 5. Hand off near the context limit

When HANDOFF PREPARATION or CONTEXT RECYCLE arrives, write the expert handoff
with the expert-handoff skill, submit it with
`agentihooks swarm <slug> handoff <doc>` and stop. The next expert of your
lineage launches on the same seat and continues from it.

## 6. End the lineage on a mission proof

When every deliverable is proven, write the mission proof from the template
below and submit it with `agentihooks swarm <slug> expert complete <proof>`.

The command refuses a proof with a fault and lists every fault: a deliverable
with no bullet, a bullet with no evidence, a bullet labelled hypothesis, a
check section with no bullet, an open item, a pull request under Deliverables
that is not merged or not written as its full link (a `#number` or an issue
link is refused there). A proof holds only what was shown. A refusal means work
is still open: do that work, then submit again. On acceptance the
lineage reads `complete`, the master is told, and the swarm closes your
session.

If the command fails after it accepted the proof, your lineage reads `complete`
while you still hold the seat, and your stop is refused. Run the same command
with the same proof again: it finishes telling the master and ends your seat.

Completion criterion: `agentihooks swarm <slug> expert status <lineage>` reads
`complete`, every inbox item you received is closed, and the session has
stopped.

## Mission proof template

Strict: exactly these eight headings in this order, the marker as the last
line. Mission opens with the mission line that step 1 printed. No file paths,
line numbers or credential values.

```markdown
# Mission proof
## Mission
The mission line.
## Deliverables
- D1 What was delivered, with its evidence: the merged pull request as its full link, the run, or the command and its output.
- D2 One bullet per deliverable of the mission, each starting with D and its number.
## Validation
- What you ran against the deployed result and what it printed.
## Smoke tests
- The smoke check on the released build, with its run or output.
## Regression tests
- The tests that hold the behaviour, with the run they are green in.
## Semgrep rules
- The rule that refuses the fault coming back, with the run its fixtures pass in.
## Feature map
- The feature map entry, with the run its grader and live check pass in.
## Open items
None
<!-- mission complete -->
```
