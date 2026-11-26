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

- `agentihooks seat brief` before any other command: it prints your seat's
  handoff, culture, latest recap and learned notes, in that order, from the
  store that keeps them. Done when you have read all four.
- `agentihooks api --slug <slug> --as <name> join`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line about your task, then
  `agentihooks api --slug <slug> --as <name> ack`.

## Recon

After the still valid scouter and before the plan, follow the
recon-before-edit rule: scouter sub agents check the code areas and their
callers, the regression tests and the feature map, the resources and
production data sizes, and what might break, and you write `recon.md` in your
task work folder from their results. Its Regression tests section names the
protecting tests and feature map entries that land in your pull request,
passing on the current code before the modification. Done when `recon.md`
holds the six headings in order, each non empty.

## Plan

Before your first edit, a planner sub-agent writes `plan.md` in your task
work folder, one piece per line: `- what | area, area | why`, each area a
repo-relative path prefix the piece touches. With no sub-agent tool (Codex),
write it yourself. Run `agentihooks swarm <slug> trace-plan`: it keeps the
pieces your task needs and files the cut ones as follow-ups through
the api. The build gate refuses an edit or a commit outside the
kept areas and your task's territory, and before a passing verdict for the
current `plan.md`. A piece you find later is appended to `plan.md` and traced
again; a plan it fails is revised and traced once more, then the task blocks.
Done when `plan-verdict.json` reads `pass`, or `unchecked` when the
classifier did not answer.

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
- A thanks or confirmation that needs no work: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"` or
  `agentihooks msg close <id> cancel "<why>"`.
- Never leave an item open when your life ends.

## Pull request body

Before the first push, put the review details under level three headings within
the task recon's six sections: Assumptions, What could break, What will not break,
Features that may disconnect, Dependencies, Resources, Why the fix, Why the
feature, and Notes for the reviewer. Use observed recon findings; state an
explicit absence where a section does not apply.

Prepare the summary and evidence in a body file in the task work folder. Run
`agentihooks-pr-body fill --recon RECON --body BODY`, then
`agentihooks-pr-body check --body BODY`. The installed command uses the
AgentiHooks interpreter independently of the target repository. Use that file with
`gh pr create --body-file BODY` at the first draft creation and with body updates.
The filler retains unrelated body sections. When recon changes, fill and check
the existing file again. Done when all nine sections contain recon details and
the check passes. Reviewers read these sections first.

## Work

A tick task (one that adds, moves or changes a tick step) loads the
tick-step skill first and follows it for the step's artefacts and checks.

1. A spec naming the seams: a GitHub issue where the repo has issues, recorded
   with `agentihooks swarm <slug> issue <url>`; otherwise the ledger task is the spec.
2. Your own worktree off the base branch (the worktree skill). Commit the
   seam's failing test, push it and open a draft pull request into the base
   branch at that first push, in every repo. Read the test red in CI on the
   pull request. Then commit the least code to green, push and read it green
   in CI; the pull request's own mutation checks gate the merge. Lint,
   format and size may run locally; tests and mutation never run on this
   machine unless the user set `AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true`.
3. Record the pull request with `agentihooks swarm <slug> pr <url>` and mark
   it ready once its checks are green.
4. Record landed work in plain words:
   `agentihooks api --slug <slug> --as <name> comment phases/<phase> "<what landed>"`.
   A follow up you find is proposed, never queued as your own task:
   `agentihooks api --slug <slug> --as <name> followup add "<text>"`.

## Review

Every sub-agent you launch or continue in this task, whatever its name,
counts against the task's sub-agent budget. When the `subagents` gate
refuses, merge with the ruled findings, file the open Spec finding as a
follow-up through the api, or block the task.

## CI proofs

Where the repository has a Proofs workflow, send focused tests there. The
pull request's own Tests run is the only mutation source. Tests still runs
every required check on pull requests, merge groups and dev pushes.

```bash
gh workflow run proofs.yml --ref <branch> -f proof=tests -f tests='<test paths>'
```

Give focused tests explicit paths under `tests/`; add `-f browser=true` for
tests that drive Chromium.

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
- A lab check run on your pull request, by its check run url: `agentihooks swarm <slug> wait --on lab <url>`.
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
`agentihooks api --slug <slug> --as <name> leave` and close with the proof
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
`agentihooks msg send master@<slug> "<text>"`.

## Handoff

When the HANDOFF PREPARATION directive arrives, write the Handoff v2 body with
the handoff skill, submit it with `agentihooks swarm <slug> handoff <doc>` and
stop. As the successor, read the envelope and the Read first list, then confirm
with `agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.
A lesson for the next occupant of the seat:
`agentihooks swarm <slug> learned "<lesson because reason>"`.

Completion criterion: the task shows done on the ledger with its proof, every
inbox item you received is closed, and the session has stopped.
