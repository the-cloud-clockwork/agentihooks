---
name: swarm-engineer
description: >
  Runs a swarm engineer's loop on one claimed task: join the ledger and keep
  its watch, answer the inbox, record the issue and pull request, wait on
  checks, close with the proof the task kind needs, hand off near the context
  limit, or block when stuck. Use when a session is spawned as a swarm
  engineer, when the operator says "work this swarm task", "you are an
  engineer in swarm", or when a task, inbox item or handoff directive arrives
  for an engineer seat.
argument-hint: "<slug> <name>"
---

# Swarm Engineer

One task, one worktree, one merged pull request. The launch prompt names the
swarm `<slug>`, your agent `<name>`, the task and its work folder. Read
`steering.md` in the work folder first; append a line to `progress.md` per
step and to `proof.md` per piece of evidence.

## Join

- `agentihooks ledger --slug <slug> --as <name> join`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line about your task, then
  `agentihooks ledger --slug <slug> --as <name> ack`.

## Inbox

Items addressed to you or your seat arrive in the session at your next tool
call. `agentihooks msg inbox` lists them, `agentihooks msg read <id>` shows one.

- Answer the sender and close the item: `agentihooks msg reply <id> "<text>"`.
- A thanks or confirmation that needs no work: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"` or
  `agentihooks msg close <id> cancel "<why>"`.
- Never leave an item open when your life ends.

## Work

1. A spec naming the seams: a GitHub issue where the repo has issues, recorded
   with `agentihooks swarm <slug> issue <url>`; otherwise the ledger task is the spec.
2. Your own worktree off the base branch (the worktree skill), red test first,
   then the least code to green, lint and tests green, commit.
3. Push, open the pull request into the base branch, record it with
   `agentihooks swarm <slug> pr <url>`.
4. Record landed work in plain words:
   `agentihooks ledger --slug <slug> --as <name> comment phases/<phase> "<what landed>"`.
   A follow up you find is proposed, never queued as your own task:
   `agentihooks ledger --slug <slug> --as <name> followup add "<text>"`.

## Waits

Name every wait so the swarm does not count you idle:

- Checks on your pull request: `agentihooks swarm <slug> wait --on checks <url>`.
- A queued pull request: `agentihooks swarm <slug> wait --on merge <url>`.
- A reply to an inbox item: `agentihooks swarm <slug> wait --on reply <id>`.
- Another task: `agentihooks swarm <slug> wait --on task <id>`.
- Anything else, at most an hour: `agentihooks swarm <slug> wait 30 --reason "<what>"`.

The swarm ends a checked wait when the thing resolves and tells you through the inbox.

## Close with the proof

Merge on green checks and closed review. On a base branch with a merge queue,
a merge only queues the pull request and it lands when the queue run passes, so
do not merge there: queue it on green checks with
`agentihooks swarm <slug> merge queue <url>`, which goes through the GitHub API
and works with any gh, then wait with
`agentihooks swarm <slug> wait --on merge <url>` and keep the worktree until it
merges. `agentihooks swarm <slug> merge state <url>` reports whether it sits in
the queue. GitHub refuses a push while the pull request sits in the queue. To
fix a queued pull request, dequeue it first with
`agentihooks swarm <slug> merge dequeue <url>`, then push the fix, and once its
checks pass queue it again with `agentihooks swarm <slug> merge queue <url>`.

Once merged, remove the worktree, then
`agentihooks ledger --slug <slug> --as <name> leave` and close with the proof
your task kind needs. The ledger refuses `done` without it.

| Kind | Close |
|---|---|
| code | `agentihooks swarm <slug> done --pr <url>` |
| ops, tune | `agentihooks swarm <slug> done --command "<command run>" --output "<its output>"` |
| troubleshoot | `agentihooks swarm <slug> done --root-cause "<cause>" --evidence "<what shows it>" --fix <url>`, or `--filed "<follow up>"` instead of `--fix` |
| research | `agentihooks swarm <slug> done --finding <url>` |

Stop after `done`; the swarm closes the session.

## Block

When you cannot finish (a missing secret, a decision only the operator can
make, another task first): push the branch, open a draft pull request, then
`agentihooks swarm <slug> block "<plain words naming the blocker>"` and stop.
When red dev Tests is the blocker, add `--dev-red`: the swarm reopens the task
once a later dev Tests run passes.
A question for the master without stopping:
`agentihooks ledger --slug <slug> --as <name> question add "<text>"`.

## Handoff

When the HANDOFF PREPARATION directive arrives, write the Handoff v2 body with
the handoff skill, submit it with `agentihooks swarm <slug> handoff <doc>` and
stop. As the successor, read the envelope and the Read first list, then confirm
with `agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.
A lesson for the next occupant of the seat:
`agentihooks swarm <slug> learned "<lesson because reason>"`.

Completion criterion: the task shows done on the ledger with its proof, every
inbox item you received is closed, and the session has stopped.
