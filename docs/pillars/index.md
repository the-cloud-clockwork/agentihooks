---
title: The Four Pillars
nav_order: 3
has_children: true
---

# The Four Pillars

AgentiHooks is organised around four pillars: Identity, Guardrails, Context Intelligence, Fleet Command. Each pillar covers one axis of running Claude Code and Codex agents.

The [swarm guide](swarm.md) covers fleet task execution, including [ready on a task of any kind](swarm.md#ready-on-a-task-of-any-kind), [ready handoffs for tasks split across pull requests](swarm.md#ready-handoffs-for-split-tasks), [labelled halves for tasks without a plan](swarm.md#labelled-halves-for-tasks-without-a-plan) and [what the ready merge pass does when a pull request leaves the merge queue](swarm.md#ready-merge-pass-queue-exits).

See [recovering a released task after red checks](swarm.md#recovering-a-released-task-after-red-checks) for the handoff and branch continuation, configuration, owner safeguards and measured recovery time.

See [proof loop health findings](swarm.md#proof-loop-health-findings) for rerun exclusions, [pull request head changes](swarm.md#pull-request-head-changes), review counts, cap configuration and verdict examples.

See [stalled activity findings](swarm.md#stalled-activity-findings) for threshold configuration, wrapper loss recovery and investigation of active agents reported as stalled.

See [stall watchdog refreshed checks](swarm.md#requeued-pull-requests-and-refreshed-checks) for requeue waits, stale failure handling, fresh GitHub reads and [watchdog configuration](swarm.md#configure-watchdog-enforcement-and-limits).

For publication retries, see [publishing unchanged files](swarm.md#publishing-unchanged-files): automatic remote comparison, skipped gates and pushes, output and missing branch handling.

For resumed work, see [publishing after a successor prepares a branch](swarm.md#publishing-after-a-successor-prepares-a-branch): branch selection, restacking, repository configuration and publication retries. See [recording a published upstream branch](swarm.md#recording-a-published-upstream-branch) for successor tracking setup, task metadata and pull request head checks.

For an existing pull request, see [continuing with one push](swarm.md#continue-an-existing-pull-request-with-one-push): retained branch selection, Stop behavior and lease failures.

See [Doctor timer pass](swarm.md#doctor-timer-pass) for what one Doctor timer step reads, its interval setting and how to measure its wall time per pass.

See [Inbox escalation sender exclusion](swarm.md#inbox-escalation-sender-exclusion) for master handoffs, escalation timing, configuration, message history and delivery evidence.

See [Inbox receiver read](swarm.md#inbox-receiver-read) for how the inbox detector resolves every receiver in a fixed number of Redis reads, the check command and the measured result.

See [Doctor measurement task readiness](swarm.md#doctor-measurement-task-readiness) for tune dependencies, application timestamps, configuration examples and releasing a read only seat with its recorded window.

See [worker execution lease through a controller rollout](swarm.md#worker-execution-lease-through-a-controller-rollout) for the lease and grant timing that keeps an Anton worker alive through a release, and why a worker exits 70.

See [one push per pull request](swarm.md#one-push-per-pull-request) for when a worker may open a pull request, when a push is refused while checks run, gate modes and troubleshooting.

See [plan tracing for promotion fixer tasks](swarm.md#plan-tracing-for-promotion-fixer-tasks) for usage, gate modes and the limits on run numbers in ledger prose, and [plan refusals in the gate log](swarm.md#plan-refusals-in-the-gate-log) for the row each refused plan writes and how Doctor rates counts it.

See [intent verdict timing](swarm.md#intent-verdict-timing) for why a ready pull request is judged before agent launches, the `at` and `persisted_at` verdict fields and how to measure ready to verdict, and [tick lock holder record](swarm.md#tick-lock-holder-record) for how a killed tick's lock is replaced at once.

See [hives](hives.md) for joining machines, roles, placement, launches on a hive, failover and fencing, and [deployment modes](../reference/deployment-modes.md) for where the controller, ledger and Redis run.

See [hive endpoint limits](hives.md#hive-endpoint-limits) for the hive endpoint request, handler and Redis time limits, the 503 it answers when Redis does not reply, and what a client does after it.

See [promotion fixer intake](swarm.md#promotion-fixer-intake) for how one swarm per repository files and closes promotion fixer tasks, hands intake over and shows its owner.

See [agent status comments](swarm.md#agent-status-comments) for when an agent's status comment amends its earlier one and when it posts a new comment at the end.

See [ledger artifacts](swarm.md#ledger-artifacts) for what the ledger accepts as an artifact, a master's plan publish, the two checks and refusal troubleshooting.

See [task ownership and master rulings](swarm.md#task-ownership-and-master-rulings) for phase and standalone task owners, ledger write permissions, claimant message refusals and fence configuration.
