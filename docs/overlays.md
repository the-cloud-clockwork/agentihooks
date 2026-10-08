---
title: Swarm Overlays
parent: Getting Started
nav_order: 10
---
# Swarm Overlays

Overlays add domain capabilities to the five swarm roles: `master`, `engineer`,
`planner`, `qa` and `cicd`. A backtest tuner remains an engineer; a risk auditor
can be an overlay for an engineer or qa. Each agent wears at most three selected
overlays, and every overlay declares which roles can wear it.

An overlay lives in a [bundle](bundles.md), the versioned Git repository for your
agent customizations. Profiles inside a project repository are not an overlay
source. The selection belongs to shared swarm configuration or task data, and
the launch record carries the overlay names and bundle revision. This lets a
Swarm v2 remote hive render the same agent from the same bundle revision.

## Guided setup

Ask an agent to use `swarm-maker`, or invoke `$swarm-maker` with the swarm's
purpose. The packaged skill asks about the outcome, proof, role capabilities,
source material and tools. It reuses your bundle or scaffolds one, builds the
overlays, validates them and explains their selection. Starting a new swarm
still requires an accepted plan through `init-swarm`.

For example: "Build a backtest swarm. Engineers need a tuner, qa needs a risk
auditor, and success means a reproducible comparison against the baseline."

## Scaffold a bundle and an overlay

Git and the installed `agentihooks` CLI are required. Check the linked bundle:

```bash
agentihooks bundle list
```

If none is linked, choose a new or empty directory:

```bash
agentihooks bundle new ~/dev/my-tools
```

This creates the bundle layout, initializes Git and links the bundle. A
nonempty directory is refused. Only one bundle can be linked at a time; reuse
it when adding more capabilities.

Create an overlay in the linked bundle:

```bash
agentihooks overlay new backtest-tuner \
  --wears engineer
```

The scaffold writes:

```text
profiles/backtest-tuner/
├── profile.yml
├── CLAUDE.md
└── .claude/
    ├── .mcp.json
    ├── skills/
    └── rules/
```

The manifest identifies the overlay and the roles it supports:

```yaml
name: backtest-tuner
description: Tunes backtests against a reproducible baseline
kind: overlay
wears: [engineer]
```

An overlay has no `extends`. Put the domain instructions in `CLAUDE.md`, reusable
skills and rules in their directories, and MCP tools in the target's native
configuration. Use environment variable references for credentials. The
scaffold refuses to overwrite an existing overlay; edit that overlay to revise
it.

Validate after editing:

```bash
agentihooks overlay check backtest-tuner
```

Fix every reported error and repeat until the command succeeds. The check
validates structure; run the capability's own loader or probe as well. Commit
and publish the bundle through its repository workflow before selecting the
overlay. Establish a remote and branch workflow for a new bundle. Remote hives
need access to the recorded commit; uncommitted laptop files are insufficient.

## Choose overlays for a swarm

Set a role default on an existing swarm:

```bash
agentihooks swarm SWARM set \
  overlays-engineer=backtest-tuner,trader
```

Use any of the five role names after `overlays-`. The page's OVERLAYS box writes
the same role defaults. Selection is limited to three overlays whose `wears`
include that role.

Override the default for one task:

```bash
agentihooks ledger \
  --slug SWARM \
  --as AGENT \
  task set TASK \
  overlays=risk-auditor
```

The task list replaces the role default. An absent task field inherits it;
`overlays=` explicitly selects no overlays for that task. Clear a role default
with `overlays-engineer=`. A new task can also take `--overlays NAME,NAME` when
the authorized planner creates it.

Changes apply at the next fresh launch. Running agents are not restarted;
relaunches and handoffs keep their recorded overlays. Check the saved selection
with `agentihooks swarm SWARM status` and the task record, then check the next
fresh agent's overlays and launch check.

The renderer appends selected overlays after the role chain and existing
always-on overlays. Different overlay sets get distinct rendered homes. The
render stamp records the worn overlays and the launch check verifies them;
the launch record preserves their names and the bundle revision for workers
on other hives.
