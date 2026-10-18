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
2. Your own worktree off the base branch (the worktree skill). Commit the
   seam's failing test and push it. In a repo with the branch mutation
   preflight, read it red on that branch push's preflight run, wait until it is
   green on the current head, then open the first draft. In any other repo,
   read it red in CI on a draft pull request into the base branch, opened at
   the first push. Then commit the least code to green, push and read it green
   in CI. Lint, format and size may run locally; tests and mutation never run
   on this machine unless the user set `AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true`.
3. Record the pull request with `agentihooks swarm <slug> pr <url>` and mark
   it ready once its checks are green.
4. Record landed work in plain words:
   `agentihooks ledger --slug <slug> --as <name> comment phases/<phase> "<what landed>"`.
   A follow up you find is proposed, never queued as your own task:
   `agentihooks ledger --slug <slug> --as <name> followup add "<text>"`.

## CI proofs

Where the repository has a Proofs workflow, send focused tests and mutation
there. Tests still runs every required check on pull requests, merge groups
and dev pushes.

```bash
gh workflow run proofs.yml --ref <branch> -f proof=tests -f tests='<test paths>'
gh workflow run proofs.yml --ref <branch> -f proof=mutation -f base=origin/dev
```

Give focused tests explicit paths under `tests/`; add `-f browser=true` for
tests that drive Chromium. Use the dependency branch as `base` for a stacked
mutation proof.

Full Tests dispatch is for gate plant proofs. Declare that intent explicitly:

```bash
gh workflow run test.yml --ref <proof branch> -f gate_plant=true
```

The admission job refuses an ordinary full dispatch and names Proofs; expensive
jobs require successful admission on dispatch. Record the proof run and head, and its expected red or
green result, before continuing.

## Waits

Name every wait so the swarm does not count you idle:

- Checks on your pull request: `agentihooks swarm <slug> wait --on checks <url>`.
- A queued pull request: `agentihooks swarm <slug> wait --on merge <url>`.
- A reply to an inbox item: `agentihooks swarm <slug> wait --on reply <id>`.
- Another task: `agentihooks swarm <slug> wait --on task <id>`.
- Anything else, at most an hour: `agentihooks swarm <slug> wait 30 --reason "<what>"`.

The swarm ends a checked wait when the thing resolves and tells you through the inbox.

## Close with the proof

A task with a pull request at delegate or full autonomy releases its seat at
`agentihooks swarm <slug> ready <doc>` once both reviews closed and the
pull request is out of draft and holds the worktree head: leave the crew first,
then ready writes the short Handoff v2 and ends your session. The tick watches
the checks, queues the pull request on green, waits for the merge, closes the
task and removes the worktree; red checks reopen it for the next engineer. A
task of another kind stays open after the merge for the next engineer to close
with its proof.

Otherwise, merge on green checks and closed review. On a base branch with a merge queue,
a merge only queues the pull request and it lands when the queue run passes, so
do not merge there: queue it on green checks with
`agentihooks swarm <slug> merge queue <url>`, which goes through the GitHub API
and works with any gh. It queues the green head as is; do not merge dev into it
first, since the merge group retests it against the newest dev. Merge dev only on
a real conflict or when GitHub refuses the enqueue. Then wait with
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
