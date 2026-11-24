---
name: expert-handoff
description: >
  Writes and reads the expert handoff, the document one expert hands the seat
  of its lineage to the next expert with: the saved mission, every deliverable
  as done or pending, the assumptions, the tests and the validation, where the
  work stopped and what comes next. Use when an expert prepares a handoff, when
  HANDOFF PREPARATION or CONTEXT RECYCLE arrives for an expert seat, when the
  handoff command refuses an expert's document, or when the next expert of a
  lineage reads the handoff it inherits.
argument-hint: "<slug> <doc>"
---

# Expert Handoff

The next expert knows only this document, the saved mission and the seat's
recaps. It must be able to continue exactly where you stopped. The handoff
command, its runtime envelope, the derived seat recap and the successor's
confirmation are the same as for every swarm agent; the body is stricter.

1. **Read the mission line.** Run `agentihooks swarm <slug> expert mission`.
   Its first line is the mission line: the address of the saved mission, then
   its title. Done when you hold that line and the number of deliverables.
2. **Write the body.** Use the template below under your `~/scratchpad` task
   folder: exactly one of each heading, in order. Done when all ten sections
   are filled, or say `None`, and the completion marker is the last line.
3. **Account for every deliverable.** Each deliverable of the mission is named
   by exactly one bullet that starts `D` and its number: under Done when it is
   proven, under Pending when anything of it is open. A deliverable half done
   is Pending, and its bullet says what is done and what is left. Done when
   `D1` to `Dn` each appear once.
4. **Carry the evidence.** Every Done, Tests and Validation bullet names what
   was observed: a pull request or issue link, a run, a commit, or a command in
   backticks with its result. A bullet you could not check carries the word
   `hypothesis` and the check still needed. Done when no such bullet is bare.
5. **Submit.** Run `agentihooks swarm <slug> handoff <doc>`. The command checks
   the structure, the mission line, the deliverable accounting, the evidence,
   the Read first addresses and prohibited content; it does not certify
   meaning. A refusal lists every fault: fix them all and submit again. After
   acceptance stop; the swarm launches the next expert on your seat. Done when
   the command printed the envelope and the transfer.
6. **Take over, as the next expert.** Read the runtime envelope, then this
   body, then each Read first address in rank order. Confirm the first Next
   action exactly as written:
   `agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.
   Verify each assumption before you depend on it. Done when the confirmation
   is recorded.

## What each section holds

| Section | Holds |
|---|---|
| Mission | The mission line first; below it, anything the next expert must know about how you read the mission. |
| Done | One `D` bullet per proven deliverable with its evidence, then other finished work with its evidence. |
| Pending | One `D` bullet per open deliverable: what is done of it, what is left, and why it is not finished. |
| Assumptions | Each thing you took as true without proving it, and how to prove it. `None` when you proved everything. |
| Tests | Each test you wrote or ran, the seam it observes and the run it was red or green in. |
| Validation | Each check against the deployed or merged result: the command and what it printed. |
| Stopped at | The last completed step and the exact state of unfinished work: branch, pull request, failing check. |
| Decisions and promises | Decisions with their reasons, promises with their recipients, questions asked and who answers. |
| Next | The first action and its completion check, then at most four more. |
| Read first | Ranked addresses, one per bullet, each followed by the question it answers. |

Read first addresses: a GitHub issue or pull request link, the mission address
from the mission line, `recap:<seat>`, `inbox:<id>`. Use only addresses that
exist. Runtime facts (seat, agent, worktree, branch, open inbox items) stay out
of the body: the envelope carries them. File paths, line numbers and credential
values never enter the body: name the module, the function or the command.

## Body template

Strict: these ten headings, once each, in this order.

```markdown
# Expert handoff
## Mission
The mission line.
## Done
- D1 What is proven, with its evidence.
## Pending
- D2 What is done of it, what is left and why.
## Assumptions
- What was taken as true, and the check that would prove it.
## Tests
- The test, the seam it observes, and the run it was red or green in.
## Validation
- The command run against the result, and what it printed.
## Stopped at
Last completed step and the exact state of unfinished work.
## Decisions and promises
Decisions and reasons, promises and recipients, open questions and who answers. None when checked and absent.
## Next
1. First action and its completion check.
## Read first
- The mission address, then the question it answers: the mission and its deliverables.
<!-- handoff complete -->
```
