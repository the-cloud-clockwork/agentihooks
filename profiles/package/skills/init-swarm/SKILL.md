---
name: init-swarm
description: >
  Turn an accepted plan into a running swarm: ledger content with phases, phase dependencies and manual or automatic
  planning, with PR sized tasks in the eng and ci lanes, then create and start the swarm. Takes
  code plans and plans for ops, troubleshooting, tuning or research work, whose
  tasks carry a kind and a proof contract. Use when the operator says
  "init swarm", "init-swarm", "start a swarm for this plan", or hands over an
  accepted plan to run with agents.
argument-hint: "<plan-file> --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N]"
---

# Init Swarm

The plan is accepted before this skill runs. This session writes the ledger
and starts the swarm, then hands the operator to the swarm's **master**: the
agent the swarm keeps online to answer the operator, keep the ledger current
and steer the swarm. This session never claims a task and does not join the
ledger as orchestrator; the master does.

## 1. Write the ledger content

Write `content.json` under `~/scratchpad/<repo>/<task>/`
(`agentihooks scratch new`, which prints the folder it built):

```json
{"title": "Delivery", "overview": "", "sources": [], "phases": [{"title": "Prepare", "description": "", "planning": "manual"}, {"title": "Build", "description": "", "depends_on": [1], "planning": "auto"}], "questions": [], "followups": []}
```

Phases follow the plan's own order. Write `depends_on` as one based phase
positions: `[1]` waits for the first phase; the ledger resolves it to `p1`.
Existing phase ids such as `p1` are also accepted. Record only dependencies the
plan names; independent phases have no dependencies. Set `planning` to `auto`
for phases the planner will slice when they open, or `manual` for phases whose
tasks the accepted plan already specifies. Omitted `planning` means manual.
Leave automatic phases without tasks.

Done when every plan phase, dependency and planning mode is present and every
source path exists.

## 2. Build the ledger

```bash
agentihooks ledger new --content <content.json> --plan <plan-file> --size swarm
```

Done when it prints the slug (`"created": true`, or the existing ledger's paths).
The slug is the swarm id below.

## 3. Add the tasks

Add tasks only to manual phases. Automatic phases stay empty: when their
dependencies finish, the tick queues one plan task in the plan lane. Its planner
slices that phase, and review approval releases its build tasks.

One task is sized for one agent in one worktree. A code task is one pull
request in the lane that owns it: `eng` for code, `ci` for workflows and
pipelines. A plan item that is not a code change (ops, troubleshooting, tuning,
research) is an `eng` task with a kind and a proof contract: read
[work-beyond-code.md](work-beyond-code.md) for the kind and the contract.

```bash
agentihooks ledger --slug <slug> task add <id> "<title>" --lane eng|ci --phase <phase> --description "<seam and done condition>" \
  [--depends-on <id>,<id>] [--territory <path or area>,<path or area>] \
  [--kind ci] [--kind ops|troubleshoot|tune|research --must "<true when done>" --check "<how>" --judge "<who>"]
```

A code task takes no `--kind` and no contract. A `ci` lane task may take
`--kind ci` for the prompt that starts red on a real workflow run; it carries no
contract either. The ledger refuses `done` on a
task beyond code until its agent posts the proof its kind needs.

The tick claims a task only once every task in `--depends-on` is done, and never
while its territory overlaps a claimed or in-review task's. Add the tasks a task
waits on first; an unknown id is refused. A task without territory never conflicts.

Done when every manual phase has at least one task, every automatic phase has
no tasks, every task names its done condition, and every task beyond code
carries its kind with `--must`, `--check` and `--judge`.

## 4. Create and start

```bash
agentihooks swarm <slug> create --repo <dir> [--template <name>] [--max-eng-agents N] [--max-ci-agents N]
agentihooks swarm <slug> status
agentihooks swarm <slug> start
agentihooks swarm <slug> status
```

After create, show the status report before start: automatic phases show
Needs a plan, or Waits for phase while dependencies remain open. Show the longest dependency chain,
parallel width and engineer width. These are dependency limits for the whole
plan; territories and agent caps may reduce concurrency. Start warns when
engineer width is below the configured engineer cap.

A template sets the caps, compact limit and per lane agent, model, effort,
role and default task kind. Use the one the operator names; list them with
`agentihooks swarm templates`. A cap flag wins over the template's cap. With
neither, the caps are 2 eng and 1 ci and init-agent picks agent and model.
Change one lane later with `agentihooks swarm <slug> set eng-model=<model>`;
`agentihooks swarm <slug> save-template <name>` keeps the result for the next plan.

Done when `status` shows the swarm running and a `master@<code>-<n>` agent
in the master lane.

## Hand over to the master

`create` and `start` end with a `Ledger page: <link>` line; `agentihooks swarm <slug> url`
prints it again. Your final message gives the operator that line as printed and the
master's name, then stop. When the line says the ledger server is not answering, run
the command it names first and print the link again. From here
the operator talks to the master in the ledger page chat or in its herdr pane;
it turns requests into tasks, sets caps, pauses or stops the swarm. A message to
the swarm from outside still works:
`agentihooks swarm <slug> send-message "<text>"`.
