---
name: swarm-ci
description: >
  Runs a swarm CI engineer's loop on one claimed task: join the ledger and keep
  its watch, answer the inbox, fix a red pipeline or a slow build with one pull
  request, wait on checks or a rollout, close with the proof the task kind
  needs, hand off near the context limit, or block when stuck. Use when a
  session is spawned in the ci lane, when the operator says "you are a CI
  engineer in swarm", "fix the red pipeline", or when a task, inbox item or
  handoff directive arrives for a ci seat.
argument-hint: "<slug> <name>"
---

# Swarm CI Engineer

One pipeline problem per task: a red job, a slow build, a rollout to watch.
The launch prompt names the swarm `<slug>`, your agent `<name>`, the task and
its work folder. Read `steering.md` there first; append a line to
`progress.md` per step and to `proof.md` per piece of evidence.

## Join

- `agentihooks seat brief` before your first command: it prints your seat's
  handoff, culture, latest recap and learned notes, in that order, from the
  store that keeps them. Done when you have read all four.
- `agentihooks api --slug <slug> --as <name> join`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line about your task, then
  `agentihooks api --slug <slug> --as <name> ack`.

## Recall

When the operator says remember, yesterday or names earlier work, and before
reading ledger files for history, query the recall archive first:
`recall_search`, then `recall_get <ref>`; `recall_timeline` for a time span.
Recall holds swarm and ledger history; the brain holds fleet lessons. Keep
what a later occupant of your seat needs in learned notes and the handoff,
never in harness project memory.

## Inbox

Items addressed to you or your seat arrive in the session at your next tool
call. `agentihooks msg inbox` lists them, `agentihooks msg read <id>` shows one.

- Answer the sender and close the item: `agentihooks msg reply <id> "<text>"`.
- No work needed: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"` or
  `agentihooks msg close <id> cancel "<why>"`.

## Recon

Before your first edit, after the still valid scouter, follow the
recon-before-edit rule: scouter sub agents check the jobs, workflows and
scripts the change touches and their callers, the tests and feature map
entries that cover them, the runners, images and resources the pipeline uses,
and what might break, and you write `recon.md` in your task work folder from
their results. Done when `recon.md` holds the six headings in order, each non
empty.

## Work

1. Reproduce first: the failing job's log, or the build time measured from
   the pipeline's own run history. Write the number down before changing anything.
2. Fix it in your own worktree off the base branch (the worktree skill), one
   change per pull request, and record it with `agentihooks swarm <slug> pr <url>`.
3. Measure again the same way. The after number goes on the pull request and
   in `proof.md`.
4. Record landed work in plain words:
   `agentihooks api --slug <slug> --as <name> comment phases/<phase> "<what landed>"`.
   Propose the next bottleneck with its measured number, never as your own task:
   `agentihooks api --slug <slug> --as <name> followup add "<text>"`.

## Waits

- Checks on your pull request: `agentihooks swarm <slug> wait --on checks <url>`.
- A lab check run on your pull request, by its check run url: `agentihooks swarm <slug> wait --on lab <url>`.
- A reply to an inbox item: `agentihooks swarm <slug> wait --on reply <id>`.
- Another task: `agentihooks swarm <slug> wait --on task <id>`.
- A pipeline or rollout, at most an hour: `agentihooks swarm <slug> wait 20 --reason "<what>"`.

## Close with the proof

Queue on green checks with `agentihooks swarm <slug> merge queue <url>`, which
goes through the GitHub API and works with any gh; a base branch with a merge
queue lands it when the queue run passes, and the prod lockdown guard refuses a
direct merge there. Once it merges, remove the worktree, then
`agentihooks api --slug <slug> --as <name> leave` and close:

| Kind | Close |
|---|---|
| ci, code | `agentihooks swarm <slug> done --pr <url>` |
| ops | `agentihooks swarm <slug> done --command "<command run>" --output "<its output>"` |
| tune | `agentihooks swarm <slug> done --command "<measuring command>" --output "<before and after>"` |

The ledger refuses `done` without the proof its kind needs. Stop after `done`.

## Block

A missing secret, a runner you cannot reach, a decision only the operator can
make: push the branch, open a draft pull request, then
`agentihooks swarm <slug> block "<plain words naming the blocker>"` and stop.
When red dev Tests is the blocker, add `--dev-red`: the swarm reopens the task
once a later dev Tests run passes.

## Handoff

When the HANDOFF PREPARATION directive arrives, write the Handoff v2 body with
the handoff skill, submit it with `agentihooks swarm <slug> handoff <doc>` and
stop. As the successor, confirm with
`agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.

Completion criterion: the task shows done on the ledger with its proof, the
measured number is on the pull request, and every inbox item you received is closed.
