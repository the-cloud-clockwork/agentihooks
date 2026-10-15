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
