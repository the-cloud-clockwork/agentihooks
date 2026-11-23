---
priority: 1
description: Work that is small in context, vague or wider than one pull request goes to a planner before any engineer or ci agent claims it; the planner writes the whole plan and every task built from it carries its slice
---

# Planners First

Applies to every role. Masters route the work, planners plan it, and engineers and ci agents claim planned work or a small self explanatory change.

- **A planner goes first.** Work that is small in context, vague, or wider than one pull request goes to a planner before any engineer or ci agent claims it. This covers operator orders and follow ups.
- **The master opens a phase with automatic planning** that carries the work in its description: `agentihooks api --slug <slug> --as <name> phase add <id> "<title>" --planning auto`. The tick queues its plan task. One thin task gets a phase of its own.
- **The master keeps `max-plan-agents` above zero while plan work is open** and focuses the plan task: `agentihooks swarm <slug> focus tasks/plan-<id>`.
- **Masters never hand write the spec** for such work; the planner writes it.
- **The planner writes the whole plan:** what will be built, code areas touched, what might break, regression tests and feature map entries, resources needed, protection plan and the done condition.
- **The planner publishes it with slice markers**, and every task built from it carries its slice.
- **Only a small self explanatory change stays a standalone task.**
- **The plan lane scales with the others.** A scale to zero is `agentihooks swarm <slug> set max-eng-agents=0 max-ci-agents=0 max-plan-agents=0`; a scale up raises all three caps together.
