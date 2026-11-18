---
title: Research
nav_order: 7
has_children: true
---

# Research

Dated evaluations behind design choices. Each page gives its verdict first, then the measurements, the sources and the commands that produced them, so a later reader can rerun the evaluation against the code as it stands then. The numbers belong to the date and window each page names; the current behaviour of the system is in [Reference](../reference/index.md).

## Pages in this section

| Page | Verdict | What it covers |
|------|---------|---------------|
| [Post merge testing with automatic revert](post-merge-testing.md) | No go: the merge queue stays | Whether a pull request should merge into `dev` on its pull request verdict alone, with `dev` graded after the merge and the culprit reverted, and how to recheck the gates in a later window |
| [Scheduler core engine choice](scheduler-core.md) | asyncio with Redis and `croniter` | APScheduler with its Redis job store against a small asyncio runner: timers, crons, durable entries, missed executions and the single runner, from primary sources |
