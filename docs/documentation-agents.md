---
title: Documentation Agents
parent: Getting Started
nav_order: 11
---
# Documentation Agents

The `documentation` package role documents delivered features and changes to
code or business logic. It uses the engineering lane and follows the project's
documentation source, audience and navigation. Its base toolset mounts the task
Playwright browser. A bundle profile or selected overlay can add capabilities.

## Start documentation work

Assign the role to a task in an existing swarm:

```bash
agentihooks ledger --slug example task add - "Document retry behavior" \
  --profile documentation \
  --description "Explain the delivered retry setting, examples and failure handling"
```

Use an existing task identifier to change its profile instead of adding another
task. A documentation agent works in a worktree, writes complete documentation,
updates navigation and verifies the project's documentation checks. It delivers
the change through the repository's normal review and pull request workflow.

The role's default `create-simple-docs` skill detects an existing documentation
source before creating anything. It extends established pages and conventions.
With no existing source or override, it uses `docs/`, numbered domain pages and
a master index. The skill belongs exclusively to this role's kit; engineer and
planner profiles do not receive it by default.

## Customize the role in a bundle

A bundle's documentation profile extends the package role:

```yaml
name: documentation
extends: [package:documentation]
description: Writes the team's public handbook
```

Its persona can specify an audience, a handbook destination or a documentation
build. A role-specific skill named `create-simple-docs` replaces the package
default. Keep that customization in the documentation profile rather than the
bundle's global kit.

An overlay can instead declare `wears: [documentation]` and be selected through
the existing [swarm overlay controls](overlays.md). Bundle instructions decide
the destination and format; the default skill follows them.

## Session completion classifier

Every harness uses the shared SessionEnd handler. SessionStart records a Git
baseline; SessionEnd gathers delivered change evidence and uses the named
`documentation-needed` classifier. Its questions are:

| Decision | Meaning |
| --- | --- |
| Documentation needed | The delivered feature or logic change needs an explanation for its users, operators or maintainers. |
| Already covered | Existing documentation sufficiently explains the new behavior and its configuration. |
| Change size | The impact is none, small, medium or large. |

Evidence includes bounded Git changes, source task requirements, discovered
documentation sources and case-insensitive regex matches for changed terms.
Documentation mentions are retrieval evidence, rather than proof of adequate
coverage. Credential files and linked documentation files are excluded, and
text is redacted before it reaches the classifier.

The default affirmative threshold is 0.7 and the negative threshold is 0.3.
Documentation is queued when the change needs documentation, existing coverage
is insufficient and the size decision is confident. Uncertain decisions remain
pending. Unchanged standalone sessions record a negative verdict without a model
call. Classifier child sessions do not create documentation work.

## Queued work and retries

In a swarm, classification runs through the detached hook worker. The new task
stays on the closing agent's own ledger, uses `profile: documentation`, retains
its source requirements and documentation evidence, and depends on the source
task. Queueing happens immediately with the source dependency, including when the
closing agent leaves CI and merge completion to the controller. Repeated
completion events use a stable documentation task identifier.

The task is added at rank low. At delegate and full autonomy, the claim queue
places a low rank the swarm set behind every other band and outside the chain
guard, so a free seat takes any claimable urgent, high or normal task first.
Below delegate the rank order alone does this. The dispatcher never proposes
such a task for a higher rank or puts it at the head of the queue. An open
documentation follow-up with no rank, such as one generated before tasks carried
rank low, sorts as low too. The task depends on its source task when that id
is a task on the ledger.

Classifier or ledger failures retain a pending request. Later bound session
completions retry a bounded number of requests from the same swarm, oldest
attempt first so retained requests do not starve later work. Documentation
tasks do not queue more documentation tasks. Session teardown continues while
the background worker performs this work.

Standalone sessions create no ledger, swarm or documentation task. They record
the verdict in the session log. When documentation is needed, the detached
worker stores a notice for the repository. The next session starting in that
repository receives one short line naming the change and how to start a
documentation agent; the notice is then marked shown.

The classifier follows the existing definition precedence: package defaults,
bundle classifier definition, then runtime classifier definition. Use the
standard classifier override mechanism to adjust its prompts or thresholds.

## Sessions that file no documentation task

A swarm session's source task comes from `AGENTIHOOKS_SWARM_TASK`. When that
value names no task on the ledger, such as the `master` seat or a task removed
since the session started, the request completes with the verdict
`no ledger task` and size `none`. No classifier call runs and no task add
reaches the ledger, so the ledger raises no refused-add sync alert. A bound
session with an empty `AGENTIHOOKS_SWARM_TASK` still classifies its changes and
queues a task with no dependency.

Change evidence is the diff from an evidence base to the head: the source task's
`branch` when the task records one that resolves, otherwise the checkout's HEAD. The checkout
is the session's worktree, or the primary repository checkout when that
worktree no longer exists. The evidence base starts at the merge base of the
SessionStart baseline and the head, then moves past `origin/dev` commits that
reached the checkout by a pull or fast forward. The first matching row applies:

| Condition at SessionEnd | Evidence base |
| --- | --- |
| No `origin/dev` ref in the repository | SessionStart baseline |
| The merge base of `origin/dev` and head is the baseline or one of its ancestors | SessionStart baseline |
| `origin/dev` commits after the baseline were pulled or fast forwarded in, and none has a `commit` entry in the checkout's HEAD reflog | Merge base of `origin/dev` and head |
| `origin/dev` commits after the baseline include one with a `commit` entry in the checkout's HEAD reflog | SessionStart baseline |
| Neither the baseline nor the merge base of `origin/dev` and head contains the other, for example after merging newer `origin/dev` into a branch | Merge base of `origin/dev` and head |

A reflog entry counts when its subject starts with `commit`, which covers
`commit`, `commit (amend)` and `commit (merge)`. The reflog decides only the
base; once the baseline is kept, the whole diff from it is counted, pulled
commits included. Commits on the head that are not on `origin/dev` always count.

A session with no source task and no changed code or documentation files left
after this records the `no changes` verdict without a model call.

### Troubleshooting

| Symptom | Cause and check |
| --- | --- |
| A master or seat session left no documentation task | Expected when `AGENTIHOOKS_SWARM_TASK` is set to an id the ledger does not hold. Its request record under `~/.agentihooks/documentation/`, named by the SHA-256 of the session id, has status `complete` and verdict source `no ledger task`. With an empty value the session is classified and can queue a task with no dependency. |
| A session that only pulled `dev` reported no changes | Expected. No pulled `origin/dev` commit has a `commit` entry in the checkout's HEAD reflog, so the evidence base moved to the merge base with `origin/dev`. |
| Delivered commits were missed | The commits were created outside this checkout, for example in another worktree, so this checkout's reflog holds no `commit` entry for them. Document the change from its own task. |
| A session that committed to `dev` and also pulled reports pulled changes | Expected. One of its commits is on `origin/dev`, so the baseline is kept and the full diff from it is counted. |
