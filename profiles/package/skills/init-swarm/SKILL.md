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

The plan is accepted before this skill runs. This session writes the ledger
and starts the swarm, then hands the operator to the swarm's **master**: the
agent the swarm keeps online to answer the operator, keep the ledger current
and steer the swarm. This session never claims a task and does not join the
ledger as orchestrator; the master does.

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
agentihooks ledger --slug <slug> task add <id> "<title>" --lane eng|ci --phase <phase> --description "<seam and done condition>" \
  [--depends-on <id>,<id>] [--territory <path or area>,<path or area>]
```

The tick claims a task only once every task in `--depends-on` is done, and never
while its territory overlaps a claimed or in-review task's. Add the tasks a task
waits on first; an unknown id is refused. A task without territory never conflicts.

Done when every phase has at least one task and every task names its done condition.

## 4. Create and start

```bash
agentihooks swarm <slug> create --repo <dir> --max-eng-agents N --max-ci-agents N
agentihooks swarm <slug> start
agentihooks swarm <slug> status
```

Done when `status` shows the swarm running and a `<slug>-master-<n>` agent
in the master lane.

## Hand over to the master

Tell the operator the ledger page and the master's name, then stop. From here
the operator talks to the master in the ledger page chat or in its herdr pane;
it turns requests into tasks, sets caps, pauses or stops the swarm. A message to
the swarm from outside still works:
`agentihooks swarm <slug> send-message "<text>"`.
