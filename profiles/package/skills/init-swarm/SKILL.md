---
name: init-swarm
description: >
  Turn an accepted plan into a running swarm: ledger content with phases, PR
  sized tasks in the eng and ci lanes, then create and start the swarm. Use when the operator says
  "init swarm", "init-swarm", "start a swarm for this plan", or hands over an
  accepted plan to run with agents.
argument-hint: "<plan-file> --repo DIR [--max-eng-agents N] [--max-ci-agents N]"
---

# Init Swarm

The plan is accepted before this skill runs. This session is the **liaison**
between the operator and the swarm: it writes the ledger, starts the swarm,
relays, and never claims a task.

## 1. Write the ledger content

Write `content.json` under `~/scratchpad/<repo>/<task>/`
(`agentihooks scratch new <repo>/<task>`):

```json
{"title": "", "overview": "", "sources": [], "phases": [{"title": "", "description": ""}], "questions": [], "followups": []}
```

Phases follow the plan's own order. Done when every plan phase is present and
every source path exists.

## 2. Build the ledger

```bash
agentihooks ledger new --content <content.json> --plan <plan-file>
```

Done when it prints the slug (`"created": true`, or the existing ledger's paths).
The slug is the swarm id below.

## 3. Add the tasks

One task is one pull request, sized for one agent in one worktree, in the lane
that owns it: `eng` for code, `ci` for workflows and pipelines.

```bash
agentihooks ledger --slug <slug> task add <id> "<title>" --lane eng|ci --phase <phase> --description "<seam and done condition>"
```

Done when every phase has at least one task and every task names its done condition.

## 4. Create and start

```bash
agentihooks swarm <slug> create --repo <dir> --max-eng-agents N --max-ci-agents N
agentihooks swarm <slug> start
agentihooks swarm <slug> status
```

Done when `status` shows the swarm running with every task open.

## Staying liaison

- Relay to the swarm chat: `agentihooks swarm <slug> send-message "<text>"`.
- Caps change by `agentihooks swarm <slug> set max-eng-agents=N max-ci-agents=N`.
