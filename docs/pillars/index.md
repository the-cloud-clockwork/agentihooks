---
title: The Four Pillars
nav_order: 3
has_children: true
---

# The Four Pillars

AgentiHooks is organised around four pillars: Identity, Guardrails, Context Intelligence, Fleet Command. Each pillar covers one axis of running Claude Code and Codex agents.

The [swarm guide](swarm.md) covers fleet task execution, including [ready handoffs for tasks split across pull requests](swarm.md#ready-handoffs-for-split-tasks) and [what the ready merge pass does when a pull request leaves the merge queue](swarm.md#ready-merge-pass-queue-exits).

See [stalled activity findings](swarm.md#stalled-activity-findings) for threshold configuration, wrapper loss recovery and investigation of active agents reported as stalled.

For resumed work, see [publishing after a successor prepares a branch](swarm.md#publishing-after-a-successor-prepares-a-branch): branch selection, restacking, repository configuration and publication retries.

For an existing pull request, see [continuing with one push](swarm.md#continue-an-existing-pull-request-with-one-push): retained branch selection, Stop behavior and lease failures.

See [plan tracing for promotion fixer tasks](swarm.md#plan-tracing-for-promotion-fixer-tasks) for usage, gate modes and the limits on run numbers in ledger prose.
