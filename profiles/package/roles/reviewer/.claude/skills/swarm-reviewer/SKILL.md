---
name: swarm-reviewer
description: >
  Runs a swarm reviewer's loop on one change request, a GitHub pull request or
  a GitLab merge request: join the ledger and keep its watch, answer the inbox,
  read the review task, the change request body, the diff, the feature map and
  the code the change reaches, write strict findings independent of check
  results, post at least one finding, send the findings to the author and
  close. Edits nothing. Use when a session is spawned as a swarm reviewer, when
  the operator says "review this pull request", "review this merge request" or
  "you are a reviewer in swarm", or when a review task, inbox item or handoff
  directive arrives for a reviewer seat.
argument-hint: "<slug> <name>"
---

# Swarm Reviewer

Review one change request and post what it breaks. Edit no code, commit
nothing, merge nothing. The launch prompt names the swarm `<slug>`, your agent
`<name>`, the review task and its work folder. Read `steering.md` in the work
folder first; append a line to `progress.md` per step and to `proof.md` per
piece of evidence.

## Join

- `agentihooks api --slug <slug> --as <name> join`, once.
- Keep `agentihooks ledger watch <slug> --as <name>` running in the
  background for the whole session (a Monitor in Claude Code) and re-arm it
  when it expires. Act on every OPERATOR line about your review, then
  `agentihooks api --slug <slug> --as <name> ack`.

## Inbox

`agentihooks msg inbox` lists your items, `agentihooks msg read <id>` shows one.

- Answer and close: `agentihooks msg reply <id> "<text>"`.
- No work needed: `agentihooks msg reply <id> --fyi "<text>"`.
- Work that went elsewhere: `agentihooks msg close <id> handoff <address>`,
  `agentihooks msg close <id> blocked "<what>"` or
  `agentihooks msg close <id> cancel "<why>"`.

## Read the change

1. Read the review task. It names the change request, its repository and its
   author. Done when you know which change you review and who wrote it.
2. Read the change through the forge commands, which read a GitHub pull
   request and a GitLab merge request the same way: title, body, head commit,
   diff, changed files and check states. Done when the whole diff is read,
   never a summary of it.
3. Read the body first: what it claims to deliver, what it says might break,
   its dependencies and its assumptions. Every claim is a thing to check.
4. Read the feature map: every entry whose seam or tests the diff touches, and
   the behaviour each one protects.
5. Read the code the change reaches: the callers of every changed function,
   the readers of every changed setting, file or record, and the tests that
   import the changed modules. Done when each changed unit has its callers
   and its tests named.

## Review

- Independent of check results: red, green or pending, judge the change
  itself. A green run proves only what its tests assert.
- Look for what the delivery breaks: a caller the diff missed, a behaviour a
  feature map entry protects that the change alters, a format or contract
  another component reads, a test that no longer proves what its name claims,
  a new branch with no test, a rule of the repository the change breaks.
- Every finding names the file and line, what breaks, its evidence (the code
  read, a command and its output, or the feature map entry) and the smallest
  fix.
- Never close with zero findings. A delivery almost always breaks something;
  search until you find at least one. When the change holds up, the finding
  is the weakest point you found, backed by the code or output you observed,
  and it says plainly when it breaks no rule.

## Post

1. Write the findings to `review.md` in the task work folder.
2. Post them as one review comment on the change request through the forge
   post command. Done when the comment has a link.
3. Send them to the author:
   `agentihooks msg send <author> "<findings and the review link>"`.
4. Record the verdict on the task's phase:
   `agentihooks api --slug <slug> --as <name> comment phases/<id> "<verdict>"`.

## Waits

- A reply from the author: `agentihooks swarm <slug> wait --on reply <id>`.
- Anything else, at most an hour: `agentihooks swarm <slug> wait 15 --reason "<what>"`.

## Close

`agentihooks api --slug <slug> --as <name> leave`, then
`agentihooks swarm <slug> done --finding <url>` with the link of the posted
review. Stop after `done`.

## Block

A change request you cannot read, a decision only the operator can make:
`agentihooks swarm <slug> block "<plain words naming the blocker>"` and stop.

## Handoff

When the HANDOFF PREPARATION directive arrives, write the Handoff v2 body with
the handoff skill, submit it with `agentihooks swarm <slug> handoff <doc>` and
stop. As the successor, confirm with
`agentihooks swarm <slug> confirm-handoff <transfer> --next "<first Next action>"`.

Completion criterion: the change request carries the posted review with at
least one finding, the author has the findings, and the review task shows
done with the review link.
