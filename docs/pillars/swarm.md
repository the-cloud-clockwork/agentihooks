---
title: "Swarm"
parent: The Four Pillars
nav_order: 8
permalink: /docs/pillars/swarm/
---

# Swarm
{: .no_toc }

Run a crew of Claude and Codex agents over the tasks of a swarm ledger, each in its own herdr pane.

1. TOC
{:toc}

---

## What a swarm is

A swarm is a herdr workspace named `<repo>-<code>`, after its repo folder and its swarm code. Its agents work the
tasks of one swarm ledger (`agentihooks ledger`), and every task belongs to a lane:

| Lane | Agent |
|---|---|
| `eng` | An engineer working a code task end to end with the dev-cycle skill. |
| `ci` | A CI engineer whose only job is CI speed; it proposes each further bottleneck as a follow up. |
| `master` | The one agent the operator talks to. It troubleshoots with read only diagnostics, plans with the operator and configures the swarm, the ledger and the operator's environment with him through the agentihooks commands and tools. It works no task and never edits code or config files in a repository, commits or merges. |

## Agent names

Run `agentihooks swarm rename` to rename every live registered swarm's agents and spaces, or
`agentihooks swarm <id> rename` for one swarm. Running it again makes no changes. Running sessions retain
their launch names; durable aliases keep inbox delivery, crew ownership, terminate selection, waits and
heartbeats working under either name. Herdr uses dashes where canonical names use an at sign, as approved
by the operator.

Every swarm agent is named `<type>@<code>-<number>`: `master@a1b2c3-0001`, `engineer@a1b2c3-0002`,
`ci@a1b2c3-0001`. The type is `master`, `engineer` or `ci`. The code is six lowercase hex characters minted once
when the swarm is created, kept on the swarm record and in a global registry that maps it to its swarm, ledger and
repo; a code another swarm holds is minted again. A swarm created before this scheme gets its code at its next
tick. The number has four digits and counts each type on its own within the swarm, never reused, so a successor
master takes the next master number. A removed swarm's code stays taken; created again, the swarm gets a new one.

That name is the Claude or Codex session name, the inbox address, the ledger crew name, the `status` row and the
`terminate-agent` target ([one Codex conversation can be stood down alone](../reference/cli-commands.md#stand-down-one-codex-conversation)).
herdr refuses an at sign in agent names, so the herdr agent name carries a dash in its place (`engineer-a1b2c3-0002`). An inbox item left for a retired engineer or ci agent moves to its
seat when its task goes on, else it is withdrawn and its sender told; a master's passes to its successor. `scripts/swarm/naming.py` is the only code that builds or parses an
agent name, and a test walks `scripts/` and `hooks/` to keep it so.

## The master

For Codex executions using `app_server`, herdr is a viewer of the managed
thread. Closing it leaves the execution running. A push master handles inbox
work and ends its turn when idle; it does not run `wait --inbox`. A swarm made
with `swarm create` uses `app_server` unless it sets `codex-inbox-transport=legacy`.
See [Managed Codex terminals](../reference/codex-managed-terminal.md) for
reopening the same thread and handoff behavior, and
[Codex inbox transport](../reference/codex-inbox-transport.md) for the setting,
the canary order and rollback. The Claude inbox channel, the Codex push pump and
broadcast topics are separate paths; that page tells them apart.

Every swarm that is not stopped keeps exactly one master, named `master@<code>-<n>`. The tick spawns it when the
swarm starts and recovers it when its execution ends (closing a managed Codex viewer alone leaves it running); it is never nudged or retired for being idle, and it
does not count against the `eng` and `ci` caps. A stopping swarm keeps its master until the last worker leaves.

Its opening prompt makes it join the ledger as orchestrator, with operator writes reaching it as inbox messages
through the recorded transport (a managed Codex master receives them in its live thread), answer every operator chat
message on the page and in its herdr pane, keep phases, follow ups, time left and status comments current, turn
operator requests into tasks with full specs, rewrite task descriptions, set caps, pause or stop the swarm,
talk to agents, and check merged UI work in a real browser, closing the shared browser after.

The master learns what agents do without watching for it: the minute tick sends it an inbox item for each agent
follow up, question, blocked task and done task, each phase it ticks or reopens, and each new health finding
(see [The minute tick](#the-minute-tick)), and the wake ladder carries every item to it asleep or awake.

The master recycles like any agent: at `AGENTIHOOKS_COMPACT_LIMIT` it writes a handoff document and runs
`agentihooks swarm <id> handoff <doc>`. The next tick retires it and spawns the next master with the document,
so one master is always online. `issue`, `pr`, `done` and `block` refuse the master. The swarm panel on the
ledger page shows the master as the first card.

Before the tick retires a live master that reported its hook, for any reason (a live binding mismatch, a failed
launch check, the idle limit or a stopping swarm), it sends that master an inbox item asking for a Handoff v2 and
waits up to `AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES` (default 5; 0 retires at once). A master that hands off is
retired and its successor starts from the document; one that does not is retired at the deadline. A record field
the tick never held (an empty harness, profile, model, effort or account) is not compared with the live process:
`swarm status` flags it as a live binding finding, and it never retires the agent.

### Master outage and promotion

When a swarm has had no live master (none whose hook reported) for `AGENTIHOOKS_MASTER_DOWN_MINUTES` (default 5),
the tick forces a master launch through its own master launch path, even after the startup retry gave up, and
forces another every window while the outage lasts. When the forced launch fails, or its master reports no hook
within two minutes, the tick promotes the oldest live engineer. The promoted engineer gets a generated inbox prompt
naming what failed; it stays on its task and seat but does not act as master: it answers no chat as master, writes
no tasks and does not steer the swarm. Its only purpose, in order: bring the master back as soon as possible, fix
the outage causes in code through its own pull requests, never as follow ups, and throughout tell the swarm and
the operator what failed and what it is doing. If it dies, the next live engineer is promoted. When a real master
binds, or the swarm stops, the tick ends the promotion and tells the engineer to return to its task. Every
promotion, prompt and hand back is a tick journal line and an event on the seat history of the master seat and the
engineer's seat. `swarm status` prints a `promoted` line, `status --json` carries `promotion` and a `promoted` flag
per agent, and the ledger page marks the agent row promoted.

### Several master seats

`swarm <id> set masters=N` gives a swarm N master seats: `master@<id>` is the lead, the address every single master
swarm already uses, then `master-2@<id>` up to `master-N@<id>`. A swarm with no count has one seat and routes as
before. Code gives every ledger phase one owning seat and saves the owners per swarm, so a restart reads the same
owners. A phase keeps its owner while its seat exists, except that a seat add or removal rebalances: an added seat
takes the busiest seats' last phases up to an even share, a removed seat's phases go to the seats owning fewest, and
every other phase keeps its owner. Items already delivered stay with the old owner. An operator write, tick item,
priority, pull request notice or task health finding about a phase or a task in it reaches the live master owning
that phase; any other item, and one whose owner seat has no live master, reaches the lead. Unaddressed chat and
`@master` stay with the lead, which answers the operator; while the lead seat is empty the lowest numbered live
master answers.

## Commands

The swarm id is a lowercase slug of letters, digits and dashes, starting with a letter, at most 48 long.

The Swarm panel's controls (`start`, `pause`, `stop`, `stop --now`, `close`, `reopen`, `set` and `lift`) take the operator or the live master of that same swarm, and refuse every engineer, ci and planner agent and every other swarm's master. Each use by the master posts one chat line on the ledger naming each control with its old and new value.

| Command | Effect |
|---|---|
| `agentihooks swarm list` | One line per swarm: state, caps, agent count, repo. |
| `agentihooks swarm tick` | One reconcile pass over every swarm (the timer runs it). |
| `agentihooks swarm <id> controller` | Show the controller owner, epoch and expiry. |
| `agentihooks swarm <id> controller release` | Release this hive's lease so another controller can take over at a higher epoch. |
| `agentihooks controller run [--once]` | Reconcile every registered swarm each minute; `--once` runs one pass. `AGENTIHOOKS_DEPLOYMENT=local` permits spawning; `compose` reconciles without spawning; `distributed` spawns only engineer and CI work its Kubernetes runtime places, and leaves master, planner, dispatcher and frontend work to the workstation hive. |
| `agentihooks swarm templates` | One line per swarm template, built-in or user: per lane its cap, agent, model, effort and default kind, then the compact limit. |
| `agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N] [--max-plan-agents N]` | Register a swarm, paused. Defaults: 2 eng, 1 ci, 1 planner. `--template` takes the caps, compact limit and lane map from a template; a cap flag still wins. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> start` | Run: enable the timer and scale up at once. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> url` | Print the `Ledger page: <link>` line, built from `LEDGER_HOST` and `LEDGER_PORT`; when the ledger server is not answering, the line names `agentihooks ledger serve --ensure`; when the server on that port serves another ledger folder, it prints no link and names both folders. |
| `agentihooks swarm <id> pause` | Stop new spawns; running agents continue. |
| `agentihooks swarm <id> proof-control --request FILE` | The creating engineer pauses, drains, sets or starts its scratch proof fixture using a JSON request. `{"control":"pause"}` pauses new spawns and `{"control":"start"}` starts the swarm. `{"control":"set","fields":{...}}` queues `swarm set` for allowlisted keys only: `codex-inbox-transport`, `max-eng-agents`, `max-ci-agents`, `max-plan-agents` and each lane's `agent`, `model`, `effort` and `profile`; each value starts with a letter or digit and holds at most 64 letters, digits, dots, colons, underscores or dashes; caps take a whole number below 1000 and the transport `legacy` or `app_server`, and each lane agent `auto`, `claude` or `codex`. Drain carries `control: "drain"`, the exact `agent`, `op`, `incarnation`, `generation` and `task_revision` from the lifecycle view. The command authenticates the engineer on its parent swarm, obtains the fixture credential internally and prints only the control result. The credential permits no other action or swarm, and set and start need a scratch swarm; drain preserves caps and fences replacement workers. |
| `agentihooks swarm <id> stop` | Drain: no new spawns, the swarm stops when its agents finish. Both forms of stop take a snapshot first. |
| `agentihooks swarm <id> stop --now` | Kill every agent and reopen its unfinished task. |
| `agentihooks swarm <id> close [--note TEXT] [--now]` | Close the ledger, with open tasks, follow ups or questions left or not: write a Summary section into the ledger overview, built from the ledger (merged tasks with their pull request links, tasks still open, follow ups still open, questions unanswered, phases done out of total, `--note` as a plain words paragraph on top), take a snapshot, retire every agent with the master last, put claimed tasks back to open, mark the ledger closed with `closed_at` and leave the swarm stopped. Settings, culture, seats and learned notes stay. While a master is live, `close` (and the ledger page's Close button, which runs the same command) hands the close to it as an inbox item instead; the master writes its paragraph and runs `close --note`. Without a live master, run by the master itself or with `--now`, it closes at once. The page then shows a Closed banner and HOME lists the ledger under Closed. |
| `agentihooks swarm <id> reopen` | Reopen a closed ledger, retaining its Summary as history and running with the kept settings. A removed swarm is recreated from its newest snapshot. The tick starts a fresh master in the master seat; its prompt carries the summary and asks it to name that summary in its first ledger chat line. Engineers start only for eligible open tasks. The Closed banner and closed HOME card both run this command. A master still retiring must exit first. With no task left to start, the swarm drains and the master stays online. |
| `agentihooks swarm <id> take-master [--replace]` | Run inside any Claude or Codex session to make it the swarm's master; the `take-master` skill runs it when the operator types "you are the master of ledger <id>". Refuses while another master is live; `--replace` retires that master first, and a dead master record is dropped. A session without `AGENTIHOOKS_AGENT_NAME` is named `master@<code>-<n>` on its session registry record, so `terminate-agent`, the tick and the inbox see it under that name. The session occupies the `master@<id>` seat and its agent record keeps the tick from spawning a second master. A closed ledger is reopened and a stopped or stopping swarm set running. Prints the full master priming: seat handoff, culture, recaps, learned notes, ledger summary and standing duties. |
| `agentihooks swarm <id> master up [--last\|--new]` | Run from a plain terminal to bring a master online. Without a flag it shows the last master (name, harness, when it last ran) and asks one question: 1 brings back the last master by reopening its own Claude or Codex conversation (`init-agent --resume`) in the `master@<id>` seat; 2 starts a new master through the tick's launch path, primed with the seat handoff, culture, recaps and learned notes. The last master is the newest master, from the agent registry or the swarm history. When it cannot be resumed (still running, no conversation id, harness not recorded, swarm repo gone, account out of quota, resume did not start) it says why and offers the new master; it never switches silently. An unclear answer is asked again, three tries in all. `--last` and `--new` answer without a prompt; `--last` that cannot resume exits non zero. A saved launch assignment or handoff launch missing a profile, harness, model or effort takes it from the swarm config: the master lane profile (default `master`), the master affinity harness (default claude) and the frontier model at an effort inside the swarm range. A live master does not stop it; both stay in the registry and the new one takes the seat. The master joins the ledger as orchestrator; a closed ledger is reopened and a stopped swarm set paused. Prints the master, pane, seat and choice as JSON; a failed launch prints the reason and exits non zero. |
| `agentihooks swarm <id> <profile> up` | Run from a plain terminal to open any role or overlay profile for yourself: a base role (`planner`, `engineer`, `qa`, `cicd`) or an installed overlay whose chain resolves to one, such as `frontend`. `master` is refused (use `master up`), as is a profile that is not installed or has no base role. It opens a quota routed Claude session in a pane of the swarm's herdr space with that profile, the inbox channel loaded, the frontier model, and `AGENTIHOOKS_SWARM` bound with lane `operator` and no task. The name comes from the swarm name registry by the role's kind (`planner@<code>-<n>`, `engineer@…`, `ci@…`) and its registry entry is marked as an operator launch. It is never put in the agent registry: it claims no task, takes no lane slot, and the tick neither nudges nor retires it; the context recycle gate skips it. It answers to you in its pane. A planner plans with you and, on your accept, appends the plan as manual phases (`ledger plan phases`), publishes it (`ledger publish-plan`), adds tasks to those phases, tells the master by inbox and ends with `swarm <id> exit`. The ledger records who appended each phase (`added_by`), and a planner holding no plan task may add tasks only to phases it appended. Other roles work with you and exit when you say so. Prints the name, pane, profile and role as JSON; a failed launch retires the name, prints the reason and exits non zero. |
| `agentihooks swarm <id> agent-up <profile>` | The same launch as `<id> <profile> up`, in its parser form. |
| `agentihooks swarm <id> exit` | An agent launched with `<profile> up` retires its own name and ends its session. Any other name is refused. |
| `agentihooks swarm <id> remove` | Delete a swarm with no agents left: its records and its watch and action counts, so a swarm created again under the same id starts from zero. |
| `agentihooks swarm <id> snapshot` | Write `~/.agentihooks/swarm/<id>/snapshot.json`: the swarm's Redis keys (config and template, agents, claims with their lease, handoffs, name counters), its seats with their history, recaps and learned notes, its culture, the inbox items, pending sets and histories of its seats and agents, a copy of the ledger and each agent's worktree path. |
| `agentihooks swarm <id> restore [--from FILE]` | After a reboot or a lost Redis: refuse while any agent of the swarm is live, write the newest snapshot back (manual, stop or automatic, by the time it was taken; `--from` names an older file) and leave the swarm paused. Each agent with a conversation id, its worktree still on disk (the master: the swarm repo) and its account not out of quota is relaunched through `init-agent --resume` into its own conversation, with the same pane name, seat, task and account (`--route`) on its lane's current model and effort, never the recorded one (a master on the frontier model at high effort); it counts as resumed only once herdr shows that conversation on the new pane, and its first message tells it to re-read its task folder and the ledger before acting. Every failed resume retains its seat and task as awaiting decision. The tick never starts a fresh replacement until the master or operator chooses Fresh on the page or runs `restore-decision`. Resume retries the old conversation; a failed retry keeps the decision open. Each agent's outcome, resumed or awaiting decision with the reason, shows as a `restored` line in `status`, under `restored` in `status --json` and in the Last restore list of the ledger page swarm panel. The ledger copy is written back only when the ledger file is gone. |
| `agentihooks swarm <id> restore-decision AGENT resume\|fresh` | The master or operator chooses how an awaiting seat returns. Resume retries the saved conversation; fresh explicitly permits a new successor. The Last restore list offers the same two controls. |
| `agentihooks swarm <id> confirm-handoff TRANSFER --next TEXT` | A live successor confirms that it read the handoff and its first Next action. Continuity remains pending until that confirmation; binding independently records live seat occupancy. Both appear in Handoff outcomes on the page and in `status --json`. |
| `agentihooks swarm <id> status [--json]` | Config, task counts, when the last automatic snapshot was taken (`snapshots  last automatic snapshot 2026-10-05 13:15 UTC  every 30 min  kept 4`), one row per agent with its [transport capabilities](#transport-capabilities), and the health findings. An agent's model and effort are what its running session last reported (the Claude status line, a Codex hook) once that differs from the launch, with model source `session`, so a manual `/model` or `/effort` switch shows on the next tick. It also prints the host room (`host room 3: <reason>`, or `host room unknown` with its reason when capacity is unavailable; see [Deployed host capacity](#deployed-host-capacity)) and, when the last spawn pass stopped at that room, the hold (`holding spawns: host memory room 3, 3 spawned since it was granted: <reason>`). The host room gates every spawn in manual and auto scaling, and the tick's master seat too, after the quota seat check. Spawns from every swarm on the host count against the room from thirty seconds before it was granted, since a new agent takes that long to show in a reading; a room held between the load watermarks keeps its first grant time, so spawns made under it keep counting. An unknown host lets spawns pass and keeps the last known room for the next reading. An operator's own master start and restore relaunches after a reboot are not gated. |
| `agentihooks swarm <id> autoscale [--fixture <file>] [--json]` | Read only: the autoscale decision (lane ceilings, pending raise, host room and reason) for the live swarm, or for a fixture of accounts and host readings. It never refreshes quota or writes the stored decision; `applied` is false when the swarm runs manual scaling. In deployed mode it reads the Kubernetes worker namespace or reports why host room is off; see [Deployed host capacity](#deployed-host-capacity). |
| `agentihooks swarm <id> names [--json]` | The swarm code, its herdr space and every agent name the swarm gave, each with its type, number, session id and spawn and retire times. |
| `agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N max-plan-agents=N` | Change the caps; `swarm <id> max-eng-agents=N` also works. |
| `agentihooks swarm <id> set masters=N` | The master seat count: `master@<id>` is the lead and answers the operator, `master-2@<id>` and up each own phases assigned by code. The tick launches a master in every empty seat after the lead. |
| `agentihooks swarm <id> set master-per=N\|hive\|off` | The master count follows the swarm size: one seat per N working agents rounded up, or one per hive (agents with no hive count as the local hive), with a floor of one; `off` (default) keeps the `masters=N` count. A larger swarm raises the count at once. A swarm smaller for ten minutes asks each extra master to hand off, and the count falls only past seats whose master has left. When an asked seat empties, its handoff and its other open items go to the lead and its retire request is withdrawn; until the count falls past it, items for its phases reach the lead. Each change rebalances phase owners and posts them on the masters channel. |
| `agentihooks swarm <id> set compact-limit=N` | Launch this swarm's next agents with `AGENTIHOOKS_COMPACT_LIMIT=N` (thousands of tokens); 0 keeps the default. |
| `agentihooks swarm <id> set autonomy=LEVEL` | Set how far agents go without the operator: `manual` engineers open a draft pull request and stop for the operator; `assist` engineers open a pull request and merge only after an operator approval line on the ledger; `delegate` (default) engineers merge on green checks; `full` is delegate, and the master also turns follow ups into tasks without asking. Agents spawned next get it in their prompt and as `AGENTIHOOKS_SWARM_AUTONOMY`; the ledger page swarm panel shows it. |
| `agentihooks swarm <id> set master-agent=claude\|codex` | Master affinity, the master lane's `agent`. The ledger page's Capacity dropdown writes the same field through Apply. When the value differs from the live master's harness, one durable inbox order reaches the master seat: hand off with Handoff v2 and stop. The tick ends the old process before it starts the successor in the same seat on the chosen harness, with the original profile and the frontier model of that harness. An unchanged value orders nothing. A failed successor launch shows on `swarm status` and the page, keeps the handoff and the seat inbox, and retries each tick. Templates, snapshots and restore carry the value. |
| `agentihooks swarm <id> set effort-min=E effort-max=E` | The effort range every lane agent starts inside, default `medium` to `xhigh`. The classifier's pick, a lane or template effort, a resume and the `init-agent` launch of a swarm lane are each clamped into it; Claude levels are `low`, `medium`, `high`, `xhigh` and `max`, and Codex levels map by rank, so Codex runs `max` as `xhigh`. Setting a lane effort outside the range is refused with the range named. The swarm panel caps row edits both ends. |
| `agentihooks swarm <id> set <gate>-gate=deny\|log only\|skip` | Operator or master only, for every gate: `identity`, `watch`, `subagents`, `reruns`, `intent`, `build`, `claim-stop`, `push-stop`, `claims`, `trace-plan`, `talk`. The mode lives in the swarm config; the ledger page's Gates controls beside Capacity write the same field, and `swarm status` reports every gate's current mode as `gate_modes`. Inside a swarm the ledger server, the tick, `swarm trace-plan` and the gate entry point read only the config, so an agent's `AGENTIHOOKS_GATE_<NAME>` is ignored there; it picks the mode only outside a swarm or when the swarm config cannot be read. `deny` refuses the call and logs the denial, `log only` lets the call through and logs the would-be denial, `skip` does not run the check; for `trace-plan`, `deny` refuses no call: it logs each failed plan and the second distinct one blocks the task, and `skip` still runs the trace and saves its verdict, with no failure row and no block. The old names `enforce`, `observe` and `off` remain aliases. Quote a mode containing spaces, for example `"watch-gate=log only"`. |
| `agentihooks swarm <id> set talk-gate=deny\|log only\|skip` | Operator or master only. The ledger server counts each eng and ci agent's talk writes (comments, chat lines, follow-ups) since its last outcome: a pushed commit, an opened pull request, resolved checks, a task state, pull request or proof change. Past 10, `deny` refuses the write and `log only` (the default) logs the would-be deny in the gate log. Talk owed to the operator and a typed `lift the talk gate` pass. The ledger hook's nudge and Stop gate also restart on an outcome and skip tool calls made inside sub-agents. |
| `agentihooks swarm <id> set intent-gate=deny\|log only\|skip` | Operator or master only. `swarm pr` writes the parent intent (project overview, phase, task) into the pull request body and arms the task's intent check. On the next tick the classifier reads that intent with the pull request title, body, changed files, the task proof and the task's plan slice, resolved the way `agentihooks plan read` resolves it (phase range, plan artifact or Swarm v2 package, without the shared sections), and judges whether the phase can use the change as delivered and whether it weakens anything the phase builds; under 0.3, or a weakening at 0.5 or more, the verdict is fail, with reasons. It also asks phase fit on its own, so a fail names its criterion: a change that fits the phase but is not usable yet fails on delivery, never as a phase mismatch. Each verdict records its stage, criterion, model and definition digest. `python -m scripts.gates.intent_calibration` measures the check on its corpus of retained and control inputs. `deny` returns a failed task to its agent (claimed again, a ledger comment and an inbox item); `log only` (the default) logs the fail in the gate log; `skip` skips the check. The `intent` gate (`python -m scripts.gates intent`) refuses `gh pr merge` and `swarm done` while the verdict is fail, or pending for under two minutes; a check still pending after two minutes passes unchecked and is counted. A pass names the plan lines it judged. A task whose plan passed intent at `swarm trace-plan` opens its pull request on that pass, with no pending hold, and the tick still judges the pull request and replaces the plan verdict. Running `swarm pr` again starts a new check. A split task's halves are judged and merged one by one; see [Intent grading of each half](#intent-grading-of-each-half). |
| `agentihooks swarm <id> set snapshot-minutes=N` | While the swarm runs, the tick writes an automatic snapshot every N minutes to `~/.agentihooks/swarm/<id>/snapshots/auto-<ms>.json` and removes the oldest past ten. Default 30; a swarm without the setting takes `AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES`; 0 turns automatic snapshots off. `snapshot.json` from `snapshot` and `stop` is separate and never pruned. |
| `agentihooks swarm <id> set eng-agent=codex eng-model=M eng-effort=E eng-kind=K eng-role=TEXT` | Change one lane field (`ci-` likewise); the next spawn in that lane uses it. |
| `agentihooks swarm <id> save-template NAME` | Write this swarm's caps, compact limit and lane map as the user template NAME. |
| `agentihooks swarm <id> send-message TEXT` | Message to every live agent's inbox; never posts to chat. |
| `agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]` | The master or the operator judges a health finding: `false-positive`, `early-real`, `established`, `insufficient-evidence` or `resolved`. The finding hides for `AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES` (60); after that it shows again, with its earlier verdict attached, only when its measure is above the measure recorded at the verdict. A standing `bottleneck-stage/<stage>` finding measures the stage's latest value in the unit of the writer that owns it, not how many times it fired: task hours for the stage detector, task hours in the window for the alert intake, minutes from push to `Gate Required` for the ci budget pass, which counts pull request Tests runs over fifteen minutes, and for a `bottleneck-stage/langfuse_account_<account>` finding the highest value to threshold ratio across that account's error rate and turn latency p95 breaches. A stage that keeps firing while its value holds or falls stays hidden after its verdict and returns once the value rises past the one judged. For example, `bottleneck-stage/merge_queue` judged `established` at 1.5 task hours stays hidden when it fires again at 1.2 and returns when it fires at 2.0. The `Fired N times` evidence line on the finding counts firings and never brings it back. Over monitoring fires past `AGENTIHOOKS_HEALTH_WATCH_MIN` (20) watch calls since the agent's last action and over `AGENTIHOOKS_HEALTH_WATCH_RATIO` (5) per action; the master, whose job is mostly watching, gets `AGENTIHOOKS_HEALTH_MASTER_WATCH_MIN` (60) and `AGENTIHOOKS_HEALTH_MASTER_WATCH_RATIO` (15). Working on drain fires when a working agent that is not idle stays on an account the quota capacity decision marks closed, or one at or under `AGENTIHOOKS_HEALTH_DRAIN_LEFT` (10) percent routing left, the quota balancer's drain line where the tick may still place work, over `AGENTIHOOKS_HEALTH_DRAIN_MINUTES` (10) after its early quota handoff warning. The `watch` gate (`python -m scripts.gates watch`) refuses the watch call that would cross the same limits, so this finding means the gate leaked; a ledger watch re-arm with no live watcher always passes. |
| `agentihooks swarm alerts` | The Grafana alert intake, run by the `agentihooks-alert-intake` user service the swarm timer installs. It takes Grafana webhook posts on `/alerts` at `AGENTIHOOKS_ALERT_INTAKE_HOST` (`0.0.0.0`) and `AGENTIHOOKS_ALERT_INTAKE_PORT` (8796; 0 turns it off), only from private addresses or the networks in `AGENTIHOOKS_ALERT_INTAKE_FROM`, and only with `Authorization: Bearer` and the token in `AGENTIHOOKS_ALERT_INTAKE_TOKEN`; a post without it gets 401 and changes nothing. It serves HTTPS from the certificate and key at `AGENTIHOOKS_ALERT_INTAKE_CERT` and `AGENTIHOOKS_ALERT_INTAKE_KEY`, and stays off without a token, or without both files on any host but loopback, so the token never crosses the network in plain text. The user service reads all three from `~/.agentihooks/.env`; when it stays off it says why in its journal and exits. An alert labelled `swarm=bottleneck` with a `ledger`, a known `stage` and `threshold_hours` keeps one standing `bottleneck-stage/<stage>` health finding on that ledger: firing updates its hours and counts each new firing once, resolved clears it. Only those labels and the `hours` value are read, never the alert text. The ci budget pass alone owns the `ci_overrun` finding, which the intake refuses: it updates that finding for each Tests run over fifteen minutes instead of adding a follow up per run. A standing finding not updated for six hours stops showing. The rules live in antoncore's Grafana provisioning. Build, ci run and launch are productive work: the intake refuses their alerts and the stage detector never raises a finding for them, though status still ranks them. The detector opens a finding only when a stage's task hours in its window pass that stage's threshold in `stage_detector.THRESHOLD_HOURS` (seat wait and review 4, quota and host hold 2, merge queue, intent fix and ci queue 1, ci overrun and launch failure 0.25). A task's claim wait starts at the latest event that made it claimable: its add, reopen or return to scope, the unfreeze or focus that admits it, its phase opening, or a dependency done. It is split into `seat_wait`, ready while every seat of its lane was busy, counting at most as many waiting tasks as the lane has seats in ready order, and `queue_wait`, every other ready instant, which raises no finding. The detector clears a finding it opened once the stage falls back under it; an alert's finding is left to its alert. Each tick, every standing stage finding over a nonzero threshold runs its remedy once per four hour window. At `delegate` or `full`, `seat_wait` moves one seat from the ci lane to the eng lane under the lane split's caps, `launch_failure` moves the first eng or ci lane pinned to the one harness the detector saw fail to the other harness and `quota_hold` does the same for the one harness its finding names as held, which each host sample records as `held_harness` when every quota held lane is pinned to that harness; a stage whose remedy is refused or has none raises its open task titled `Bottleneck fix for the <stage> stage` to `urgent`, which the dispatcher queue orders first, and an urgent one needs nothing more. Otherwise, and for every stage below `delegate`, the master gets one proposed task carrying the hours: to add that task, or to raise the open one. Each action goes to the gate log as rule `bottleneck-remedy` with the hours it targeted and is kept as the stage's last action. |
| `agentihooks swarm <id> classify EXECUTION RULING --reason TEXT` | The master or the operator rules `lost` or `working` on an execution attempt the observer flagged for the operator under `AGENTIHOOKS_OBSERVATION_MODE=conservative`. The reason, author and time are kept on its observation. `lost` holds the attempt lost and writes the audit record; `working` holds it working until a newer loss reading (pod gone or ended, supervisor exited) flags it again, or it recovers on its own. Only a registered master record or the operator acts: any other name, an empty reason or an attempt that is not flagged is refused. `verdict` follows the same authority rule. |
| `agentihooks swarm <id> terminal EXECUTION` | For a local managed Codex execution, reopen a native viewer on its recorded thread; see [Managed Codex terminals](../reference/codex-managed-terminal.md). For Kubernetes, the operator opens the worker terminal in herdr. The command takes only the swarm and the execution: the host, the generation and the host key come from the record the controller wrote when it launched the Pod. It signs a short lived certificate with the user CA key named by `AGENTIHOOKS_TERMINAL_CA_KEY`, reads the worker namespace from the Pod policy in `AGENTIHOOKS_POD_POLICY_FILE`, writes the SSH material under the operator's own home and saves the machine in herdr. The Kubernetes path refuses a master or any agent session, and an execution with no recorded host key. |
| `agentihooks swarm-terminal setup [--antoncore DIR]`, `agentihooks swarm-terminal install` | Places the files the terminal command needs on the operator machine, with nobody copying them by hand. `setup` reads the user CA key from OpenBao `secret/agentihooks-swarm/terminal-attach` field `user_ca` with `OPENBAO_URL` and `OPENBAO_TOKEN`, and the Pod policy from the `swarm-pod-policy` ConfigMap in the antoncore checkout (`--antoncore`, else `AGENTIHOOKS_ANTONCORE_DIR`, else `~/dev/tcc-ecosystem/antoncore`). Each is checked before it replaces the previous file: an Ed25519 OpenSSH key, a valid policy. It writes `user_ca`, `pod-policy.json` and `terminal.env`, which names both as `AGENTIHOOKS_TERMINAL_CA_KEY` and `AGENTIHOOKS_POD_POLICY_FILE`, mode 0600 under `~/.agentihooks/swarm-terminal`, below the `*.env` autoload, and prints only their paths, the CA fingerprint and the worker namespace. `install` writes and enables the `agentihooks-swarm-terminal` user timer, which runs `setup` a minute after boot and every fifteen minutes. Both refuse inside an agent session. `operator_setup.settings` reads `terminal.env` back for the service that uses the files. The workstation reads controller host key records through `scripts.swarm_v2.herdr.publications`, which opens `AGENTIHOOKS_HIVE_REDIS_URL`, else `AGENTIHOOKS_SWARM_REDIS_URL`, only as `rediss` with the certificate and its hostname verified, refuses a URL carrying `ssl_` options and a host resolving outside the private network, and keeps each worker's host key exactly as the controller recorded it at launch. |
| `agentihooks swarm <id> lift AGENT GATE` | The operator or the master lets one agent past one gate for an hour, the page's lift button in the Agents table runs it. It arms the lift for that agent and writes the same `lift` row to the gate log as typing `lift the <gate> gate` in the agent's pane; the hook gates and the talk gate honour it. Each agent in `status --json` lists `gates`: every gate that denied it in the last hour and whether a lift holds. Other agents are refused. |
| `agentihooks swarm <id> cover STAGE ITEM --reason TEXT` | Marks a bottleneck stage (`seat_wait`, `ci_overrun`, `merge_queue`, or a bottleneck share such as `review`) owned by an open `phases/<id>` or `tasks/<id>`, with the reason; a spoken name such as `merge queue` is accepted, an unknown name is refused with the names it takes, and a new cover on a stage replaces the old one. A cover on a share covers every stage inside it (`review` holds `review`, `merge_queue` and `intent_fix`; `quota` holds `quota_hold`; `host` holds `host_hold`), and a cover on a stage frees its share's dispatcher seat but leaves sibling stages' remedies running. A stage with a standing finding is also taken under its exact name. While that item is open the bottleneck hunter proposes no task and takes no remedy for the stage, and the bottleneck trigger for that share does not hold the dispatcher seat open. The cover lapses and is forgotten once the item is done, out of scope, or gone from a ledger that still lists others of its kind; the stage then fires normally. The operator, the master or the dispatcher writes it; a closed or missing item is refused, and so is every other agent. |
| `agentihooks swarm <id> freeze TARGET`, `agentihooks swarm <id> focus TARGET`, `agentihooks swarm <id> unfreeze TARGET` `[--reason TEXT] [--quote WORDS]` | Writes a freeze or focus record to the ledger's `freezes` collection, or clears one. A target is a node address (`plans/<id>`, `phases/<id>`, `slices/<id>`, `tasks/<id>`) or a selector `lane:<lane>` or `kind:<kind>`. A freeze holds the node and everything under it; a focus holds everything outside it. `unfreeze` on a node clears only the records on that node; records on nodes under it stay until each is cleared. The operator writes them; the master writes them only with `--quote` carrying the operator's recorded words; the dispatcher writes them only at `full` autonomy. Every other agent is refused. `status` lists each active record. A Doctor swarm also follows the ledger it watches: while that ledger has a focus, Doctor tasks outside the fix phase are held and fixes pass; a freeze there does not touch the Doctor, and its detectors keep running. |
| `agentihooks swarm <id> learned` | List every seat's learned notes, one line each: seat, number, maturity, text. |
| `agentihooks swarm <id> promote SEAT NUMBER MATURITY --reason TEXT` | Raise learned note NUMBER on SEAT (`eng-1` or `eng-1@<id>`) to a higher maturity, keeping who promoted it and why. Any agent of the swarm or the operator may promote to `insight`; only the master or the operator to `canon`. |
| `agentihooks swarm <id> retire SEAT NUMBER --reason TEXT` | Retire learned note NUMBER on SEAT so no later occupant's opening prompt carries it; the note keeps its number and records who retired it and why. Only the master or the operator. |
| `agentihooks swarm <id> culture set FILE` | Replace the swarm's culture with the file's text. Every new occupant of every seat, the master included, reads it in its priming chain. |
| `agentihooks swarm <id> culture show` | Print the swarm's culture. |
| `agentihooks swarm <id> masters-channel say TEXT`, `agentihooks swarm <id> masters-channel read [--all]` | The masters channel (`masters.<id>`): only a session holding one of the swarm's master seats posts and reads; every other caller is refused. Posts are kept in Redis with no expiry. Each `say` post leaves every other occupied master seat an information inbox item from the poster that names the post and the read command, so the other masters receive it at their next tool call; the text stays in the channel. `read` prints the posts the seat has not read and moves the seat's cursor, which its successor inherits; `--all` prints every post. `channel_publish` and `agentihooks channel publish` refuse every `masters.` name, so no post reaches the shared broadcast file. The tick posts each master count change there. |

### Templates

A template is a JSON file: a `name`, a `compact_limit`, optional `links`, an optional `autonomy`
(`manual`, `assist`, `delegate` or `full`; empty means `delegate`) the swarm is created with, and `lanes` with one entry per lane. Worker lanes are `eng`, `ci` and `plan`; `master` configures the master. The `plan` lane defaults to the `planner` profile and has its own cap, names `planner@<code>-<n>` and seats `plan-<k>@<slug>`. A swarm without planner tasks starts no planners. A Claude planner starts in plan mode (`--permission-mode plan`; `agentihooks claude` then passes `--allow-dangerously-skip-permissions`, since `--dangerously-skip-permissions` would override the named mode) and leaves it with no pane prompt: the PermissionRequest hook allows its `ExitPlanMode` with the plan as the updated input and sets the session back to bypass, because Claude Code keeps the proceed prompt for a bare allow and would otherwise fall back to manual mode. The plan is approved by the plan review below. Codex has no launch flag for plan mode, so a Codex planner launches under a `planner` permissions profile instead (`-c permissions.planner=…` with `-c default_permissions="planner"`): it extends `:read-only`, turns network on and grants write only to the ledger folder (`LEDGER_DIR`, default `~/development-ledger`), `~/.agentihooks/swarm` and `~/scratchpad`. The repository stays read only while `agentihooks ledger` and Redis calls work; `ledger task add` needs the ledger folder because it takes a lock file there. The `trace-plan` and `build` gates stay the second line. Per lane:

| Field | Meaning |
|---|---|
| `role` | Replaces the lane's default role text in the agent's opening prompt; empty keeps the default. |
| `cap` | The lane cap the swarm is created with. |
| `agent` | `claude`, `codex` or `auto`; `claude` and `codex` always spawn that harness; `auto` takes the harness of the account the session rotation picks: the eligible account with the fewest live sessions under its five hour band. Codex is one more account in the rotation, judged on its week alone with the top band. |
| `model`, `effort` | Passed to init-agent for the lane's agents. An `auto` effort on an eng or ci lane takes the model pick inside the task's size and rank band (see the classifier reference); elsewhere `auto` keeps init-agent's default, `medium`. The master lane ignores both: a master always launches on the frontier model at high effort for its harness, opus or gpt-6.1-sol. |
| `kind` | The kind written to a task of this lane that has none when the tick claims it; `auto` writes nothing. |

`links` is a list of `{"from", "to", "kind"}`. `from` and `to` name a seat (`eng-1`) or a lane (`eng`,
`ci`); `kind` is `delegates-to` or `can-observe`. A swarm created from the template keeps them. When it
has any, a sender holding one of its seats may `msg send` or `swarm say` to another of its seats only
along a `delegates-to` link; a `can-observe` link refuses the send and names why. Either kind lets the
sender read that seat's items with `agentihooks msg inbox --of <seat>`. The operator, the master, a
sender holding no seat, and a swarm without links are never restricted.

Built-in templates (`default`, `codex-ci`, `codex-only`, `claude-only`) ship with agentihooks.
`codex-only` and `claude-only` pin `agent` on the eng, ci, plan and master lanes, so every seat runs on
one harness; a Claude only profile on a `codex-only` lane is refused with its reason. Where nothing pins a
harness, a launch takes the harness with an open seat, and with no open seat on either harness it is
refused rather than defaulting to Claude. A task whose profile is `frontend` takes an open Claude seat
first and a Codex seat only when no Claude seat is open, both in the quota plan and in the spawn rotation;
a pinned lane or a saved harness still wins. User templates live in
`$AGENTIHOOKS_HOME/swarm-templates/` and win over a built-in of the same name. `create --template` stores
the template name and lane map in the swarm config.

A profile is Claude only when a layer of its own chain enables a Claude plugin that has no Codex
replacement; role default plugins and the bundle global layer never count. The replacements are:

| Claude plugin | Codex replacement |
|---|---|
| `mattpocock-skills` | the installed plugin's own skills |
| `frontend-design` | a `frontend-design` skill in a chain layer's `.codex/skills/` (the bundle's `frontend` profile ships one) or fetched into `~/.agentihooks/codex-skills/` |
| `impeccable` | the `impeccable` skill fetched into `~/.agentihooks/codex-skills/` by the bundle's `impeccable-codex` dependency |
| `playwright` | the swarm browser MCP, which every base role chain carries |

The Codex render links each replacement into the profile's Codex `skills/` folder and records it in the
render stamp, so a later fetch re-renders the home. A plugin with none of these keeps the profile on
Claude, and a `codex-only` lane refuses it.

A swarm is in one of five states: `running`, `paused`, `stopping`, `stopped`, `drained`. It drains when no
task is left to start and returns to `running` when a new task opens. Lowering a cap never kills work; the
count falls as agents finish.

Agent commands take the agent name from `--as` or `AGENTIHOOKS_AGENT_NAME`:

| Command | Effect |
|---|---|
| `agentihooks swarm <id> issue URL` | Record the task's GitHub issue, where the repo has issues; without them the ledger task is the spec. |
| `agentihooks swarm <id> pr URL` | Record the task's pull request, its head branch and that branch's repository (`branch_repo`); the task moves to `pr`. |
| `agentihooks swarm <id> merge queue\|dequeue\|state URL` | Queue a pull request into `dev` on the merge queue at its current head, take it out of the queue, or report its state, head and queue entry as JSON. `queue` reads the newest `pull_request` run of the Tests workflow on the current head and refuses unless that run passed (`the current pull request head must pass Tests before queueing`); it then enqueues that exact head with `expectedHeadOid`. It never compares the branch with `dev`, merges `dev` into it or updates it, whatever changed on `dev` since the checks ran: the merge group runs the unit suite on the head merged with the newest `dev` ([Delivery tiers](../reference/delivery-tiers.md#entering-the-merge-queue)). A repository with no merge queue on `dev` gets a squash merge instead, only for a ready pull request with green checks and a review decision of none or approved. A pull request already in the queue returns its state without another check or enqueue. It goes through `gh api graphql`, so any `gh` with `gh api` works; queue and dequeue run only for a swarm worker on its task's recorded pull request, never the master, and refuse a pull request into any other base. |
| `agentihooks swarm <id> prepare` | Set `--repositories /absolute/repo,/absolute/other` when adding a task, or `task set <task> repositories=/absolute/repo`. Use that explicit `repositories` list of local repository paths; without it, report that the swarm repository is the default. Prose repository names do not select repositories. Create or reuse one worktree per repository from its own `dev`, with a recorded continuation branch scoped to that repository. Return each worktree and branch. |
| `agentihooks swarm <id> publish --message TEXT -- FILE...` | Use the repository of the current task worktree; from a primary checkout, resolve the sole task repository or require a worktree selection when several are declared. Stage only explicit repository relative files, commit, run that repository’s `scripts.ci_prepush` gate, then push and retain its branch separately from other task repositories. The result names the gate. A repository without that module uses the named AgentiHooks touched Python fallback: lint, format, size and complexity on changed Python files since its own `origin/dev` merge base, without agentihooks baseline files or local tests. Unrelated tracked changes are refused before staging; unrelated untracked files are preserved. A failed stage stops publication and names the next command. Repeat the command after fixing a failure; an already committed head is graded again without an empty commit. Protected branches and tasks owned by another agent are refused. Tests run only in CI. |
| `agentihooks swarm <id> branch` | Record the current worktree branch on the task once it is on origin, with its repository (`branch_repo`, the origin remote without credentials), so dependent tasks can start from it. |
| `agentihooks swarm <id> review` | Start a review round on the task. The third round answers that review is closed: any finding still open gets one ruling from the other reader, then the merge proceeds. A fourth is refused with that text. |
| `agentihooks swarm <id> done [--pr URL] [proof flags]` | Close the task with the proof its kind needs; the swarm then closes the session. |
| `agentihooks swarm <id> merge priority URL` | The master or operator moves a green critical pull request ahead of waiting dev queue entries. Running entries stay in place. Waiting entries ahead are removed tail first and requeued in their original order after the critical request; their swarm owners receive an inbox message. Entries behind an already queued critical request stay in place. A failed insertion still attempts to restore every displaced entry and reports any restoration failure. |
| `agentihooks swarm <id> ready DOC` | Release the seat of a code or ci task whose reviewed pull request waits only on checks and the merge queue. Refuses at manual or assist autonomy, without a recorded open pull request, or unless the worktree is clean and its head is the pull request head. Checks the document like `handoff`, records the pull request, head and worktree, marks the task `awaiting` `merge` (still `pr`), stores the handoff and ends the session; the finished agent no longer holds a seat and the tick never reopens the task as an orphan. Each tick the merge pass watches its checks like a checks wait, queues its green head as is like `merge queue`, waits on the queue like a merge wait, then writes the same done fields `done` writes, closes its group members and removes the worktree with `wt.sh done`. Red checks, a closed pull request or a queue ejection reopen the task for the next engineer with the handoff, removing the worktree with `wt.sh done --pushed`. Five refusals GitHub returns (a GraphQL error, or a base other than `dev`) reopen it too; any other queue failure, an outage or checks still running, is retried and never counted. The intent gate holds `ready` like `done` in its enforce and coach modes; off and observe let it pass without a verdict. |
| `agentihooks swarm <id> block NOTE` | Comment the blocker, mark the task `blocked`, end the session. |
| `agentihooks swarm <id> trace-plan` | Trace `plan.md` in your task work folder, one piece per line (`- what \| area, area \| why`), to the task, its phase and the project intent through the classifier. A piece under 0.3 is cut and, once the plan passes, filed as a follow-up. The task's latest five scope authorizations, master relays of operator words and operator or master comments starting `In scope:`, travel in the trace intent, and the classifier asks whether each covers each piece that is not a clearance; a piece covered at 0.7 is kept, its row and the report's `authorized` name the authorization and it is never filed as a cut. A new authorization retraces an unchanged plan without counting a failed plan; a follow up filed before the authorization stays filed. See [Task scope authorizations](#task-scope-authorizations) for recording scope, report examples and troubleshooting. A plan with more than half its pieces cut, or sized above one pull request at confidence 0.7, fails; the second failed plan blocks the task when `trace-plan-gate=deny` (default `log only` logs it). A piece appended to a passing plan is traced alone. Before the task's pull request opens, a plan that does not fail is also judged by the intent check as a plan, on its kept pieces and plan slice: whether the planned work serves the phase, covers the task and weakens nothing, with no demand for shipped code or proof; that verdict is printed as `intent` and logged like a pull request verdict. The verdict lands in `plan-verdict.json`; no classifier answer gives `unchecked`, counted in the gate log. The `build` gate (`python -m scripts.gates build`, on Edit, Write, MultiEdit, NotebookEdit, Serena edit tools and `git commit`) refuses an engineer's or CI agent's edit before a passing verdict for the current `plan.md`, and an edit or staged file outside the kept pieces' areas and the task territory; the work folder, `~/scratchpad` and files outside any git work tree are exempt, and an `unchecked` verdict lets edits through, counted. `build-gate` defaults to `log only`. |
| `agentihooks swarm <id> plan approve PHASE [--note TEXT]` / `plan send-back PHASE --note TEXT` | Decide the slice of a phase in review. At `manual` and `assist` autonomy the operator decides with the **Approve plan** and **Send back** buttons on the phase row, and the command refuses an agent naming them (at `assist` the master posts its recommendation as a phase comment). At `delegate` and `full` only the master decides. Approve moves the phase to building and the tick claims its tasks on the same pass. Send back reopens the plan task, records the note for the planner's steering and counts a round; when the planner finishes again the tick reopens the review. The third send back leaves the plan task done, marks the review escalated and raises a priority naming the notes; only the operator decides after that, and an operator send back reopens the plan task whatever the count. |
| `agentihooks swarm <id> wait [MINUTES] [--on checks PR_URL\|RUN_URL\|reply ITEM\|task ID] [--reason TEXT]` | Declare a wait. While it holds, the tick counts no idle tick for you, so you are neither nudged nor retired. A checked wait (`--on`) lasts until the tick sees its thing resolve (the checks finish or the pull request closes or merges, an Actions run completes, is cancelled or is missing, the inbox item closes, the task is done or blocked), at most MINUTES or 12 hours; the tick then ends it and sends you an inbox item, which for a red Actions run names its conclusion and each failed, timed out or cancelled job, or says its jobs could not be read. A bare wait lasts MINUTES, at most 60. The `claim-stop` gate (`python -m scripts.gates claim-stop`, run from a Stop condition) refuses an eng or ci agent's stop while it holds a claimed or pr task with work owed: its pull request merged (run `done`), checks failed (fix or block) or green (merge), or no pull request and no live wait. Pending checks pass the stop and record a checked wait on them, unless a live wait on a reply or a task already holds, which is kept with its reason. The third block in a row with no outcome between blocks the task. The `push-stop` gate, beside it in the engineer, cicd and qa roles, pushes the agent's task branches itself (any worktree `~/dev/worktrees/<repo>/<agent>[-N]` on its own branch, never `dev` or `main`), records branch and head on the task, and refuses the stop with one fixed message, also sent to the agent's inbox, over uncommitted changes, a push origin refused, or pushed work with no pull request and no ledger line since the push. Planners, plan tasks, the master and the operator's sessions pass. |
| `agentihooks swarm <id> progress --doing TEXT --ends-when TEXT` | Say what you are doing and when it ends: a ledger line on your task, a line in its work folder `progress.md` and an outcome on the progress signal. The tick raises a quiet flag (`gates/quiet/<agent>`, a gate log `count` row) on an eng or ci agent holding a claimed or pr task with no progress signal (last outcome, work folder progress line, start) for 30 minutes and no live checked wait. While it stands the `quiet` gate (`python -m scripts.gates quiet`, behind a bash flag test on every tool call) refuses every call except `agentihooks ledger`, `swarm` and `msg` commands; `progress` clears it. The stale claim finding reads the same quiet minutes, and each idle tick is a gate log `count` row (`idle-ticks`) for `doctor rates`. |
| `agentihooks swarm <id> handoff DOC [--recap FILE]` | Finish the session but keep the task: the next tick spawns a successor with the document in its prompt. `--recap` adds the recap (what you did, where you stopped, what you promised) to your seat; older recaps are kept. A hook asks for it when the session reaches `AGENTIHOOKS_COMPACT_LIMIT` thousand tokens (default 600). |
| `agentihooks swarm <id> park DOC` | Park a stacked task whose dependency is still open. Refuses unless the task's `branch` is on origin at the worktree head, the worktree holds no uncommitted changes or untracked files and, where the repo has issues, the task has an issue. Checks the document like `handoff`, writes `parked_on` (the open dependencies) and `stacked_base` (where the branch left the newest dependency branch; for a dependency whose `branch_repo` is another repository, fetched from there, the task's own fork from `origin/dev`), records those other repositories in `parked_repos`, comments the issue with the branch and the ledger task with the blocker, stores the handoff, ends the session and removes the worktree with `wt.sh done --pushed`, which keeps the pushed branch, since the next engineer cuts its own from the pushed branch. The tick reopens the task, which waits until its blockers are done. |
| `agentihooks swarm <id> restack` | After the parked blockers finish, fetch origin and rebase task work from `stacked_base` onto `origin/dev`. Run in a clean branch cut from the parked branch. Success clears `parked_on`; a conflict lists the files and retains the parked list. Resolve the files, run `git rebase --continue`, then run restack again to finish. |
| `agentihooks swarm <id> learned TEXT [--maturity data\|note\|insight\|canon]` | Add a lesson to your seat's learned notes, kept for every later occupant. The maturity defaults to `note`; only the master writes `canon`. |
| `agentihooks swarm <id> say TEXT [--to NAME\|eng\|ci]` | Post to the swarm chat. |

## Task scope authorizations

Record extra scope on the task before the engineer retraces its plan. This lets
an authorized piece remain in the plan even when the task description omits it.
The tracer reads the task's comments alongside the project, phase and task
intent; it does not require rewriting the description.

### Record the authorization

The tracer recognizes these task comments:

| Record | Requirement |
|---|---|
| Master relay of the operator's words | Record it on `tasks/<task>` with `ledger relay`; the stored comment has `relayed_by`. |
| Master or operator scope comment | Start the comment with `In scope:`. Leading whitespace and letter case are ignored. |

Only the latest five qualifying, nondeleted comments are included, in their
stored order. Ordinary comments, engineer comments marked `In scope:`, phase
comments and page chat do not supply task authorizations. Question answers are
not read because ledger questions carry no task link. Put an authorization from
an answer onto the task through one of the recognized records.

For example, a master authorizes removal of a size allowance while the task
description only mentions fixing the size gate. Run this as the task's master,
using the actual ledger, master name and task:

```bash
agentihooks ledger \
  --slug example-swarm \
  --as master@a1b2c3-0001 \
  comment tasks/t747 \
  "In scope: Remove the size allowance while fixing the size gate."
```

When the operator authorized that work in the master's pane, the master can
record the operator's words instead. Relay actual words received from the
operator:

```bash
agentihooks ledger \
  --slug example-swarm \
  --as master@a1b2c3-0001 \
  relay tasks/t747 \
  "Remove the size allowance while fixing the size gate." \
  --quote "Remove the size allowance too."
```

### Retrace and inspect the result

In the engineer's task work folder, include the authorized piece in `plan.md`
using the usual `what | areas | why` format. The area is a repository relative
prefix for the work it permits, for example:

```text
- Remove the size allowance | scripts/ci_prepush | The master authorized removal while fixing the size gate
```

From the task worktree, run:

```bash
agentihooks swarm example-swarm trace-plan
```

For each newly judged piece that is not a mutation clearance, the classifier
asks whether each included authorization covers it. Coverage at or above `0.7`
keeps the piece even if its task relevance score would otherwise cut it. The
piece counts as kept when checking whether more than half the plan was cut,
and it is excluded from new cut follow ups. Naming an authorization in the
plan's reason provides context; the classifier still judges coverage.

The command's report names the authorization comment ID, rather than repeating
its text. An illustrative excerpt is:

```json
{
  "kept": ["Remove the size allowance"],
  "cut": [],
  "authorized": [
    {
      "what": "Remove the size allowance",
      "authorization": "add-example"
    }
  ]
}
```

The corresponding piece in `plan-verdict.json` has
`"authorized": "add-example"`. Match that ID to the task comment to see who
authorized the work and what they approved. The report's `authorized` list
contains pieces kept through authorization, rather than every kept piece.

A changed authorization list causes an unchanged plan to be traced again;
retracing that same plan does not add another failed-plan attempt. Follow ups
filed before the authorization remain on the ledger. Authorization does not
waive the plan size check, the intent check or the build gate's area and task
territory checks.

### Configuration and troubleshooting

Authorization requires no separate enable flag. The packaged `trace-plan`
[classifier definition](../reference/classifier.md) supplies the `covered: 0.7`
threshold. The master or operator controls enforcement through the swarm's gate
settings, for example:

```bash
agentihooks swarm example-swarm set \
  trace-plan-gate=deny \
  build-gate=deny
```

Both gates default to `log only`. In `deny` mode the second failed plan blocks
the task; the build gate requires a current passing plan and permits only its
kept areas within task territory. Gate modes control enforcement, not whether
a comment grants scope.

| Symptom | Check or action |
|---|---|
| An authorized piece is still cut | Confirm the authorization is on this task, has a recognized author and marker or relay metadata, is not deleted, and is among the latest five qualifying comments. Make its wording specific enough to cover the piece, then retrace. |
| The report has no authorization for a kept piece | It may have been kept by ordinary task relevance; inspect the piece row and task comments. |
| A prior cut follow up is still present | Retracing does not close existing follow ups. The master decides their disposition through the normal follow up workflow. |
| The plan still fails after the piece is kept | Read `reasons`: authorized scope can still exceed one pull request. Narrow the plan without discarding the authorization record. |
| The report says `unchecked` | The classifier did not answer. Edits are allowed and counted; run tracing again when the classifier is available. |

## Publishing after a successor prepares a branch

`swarm publish` selects a branch from the prepared checkout and its continuation
record. A successor can therefore publish after restacking parked work onto
`dev`, even when that rebase means its head no longer descends from the parked
predecessor's remote branch.

The branch choice follows Git ancestry, rather than simply reusing the previous
worker's branch name:

| Checkout and continuation state | Publication target |
|---|---|
| An eligible recorded continuation's local `origin` ref is an ancestor of `HEAD` | Keep that continuation branch. A fast forward continuation can keep updating an existing pull request. |
| Restacking removed that ancestry, and no other eligible recorded branch passes the ancestry check | Use the prepared checkout's own branch. The parked predecessor branch stays at its previous head. |
| No recorded branch exists, or its local `origin` ref is missing | Use the prepared checkout's own branch unless another recorded candidate passes the ancestry check. A missing ref does not print a fatal Git diagnostic. |

The current implementation checks a retained continuation from the handoff or
reclaim record first, then the branch supplied by repository and checkout
selection. Each candidate must pass the ancestry check. Inside a task worktree,
checkout selection supplies the local branch; preserving a predecessor target
there requires a retained handoff or reclaim continuation. From a primary
checkout, selection can instead supply the task's recorded repository branch.
In those continuation contexts, a different local branch name alone does not
require a new remote branch: even parked work stays on an eligible recorded
branch when it still fast forwards, including when there were no task commits
beyond the stacked base.

After a successful push, the result's `branch` names the actual target and
`head` names the published commit. The task records that target in `branch` and
in `repository_branches` for the selected repository; `branch_repo` identifies
its origin repository. The push sets the checkout's upstream to the chosen
remote branch. Publication does not retarget or open a pull request: record the
pull request for the published branch with `swarm pr`.

### Resume parked work and publish

Use this sequence as the task's current worker, after reading its handoff and
after its parked dependencies have finished. Replace angle bracket arguments
with the swarm name and explicit files you changed. Worktree and branch names
come from `prepare`.

```bash
agentihooks swarm <id> prepare
```

Change into the returned worktree. For parked work, restack from a clean
checkout before making further changes:

```bash
cd <returned-worktree>
agentihooks swarm <id> restack
```

A rebase conflict leaves the parked list intact. Resolve the reported files,
stage the resolved files explicitly, run `git rebase --continue`, and run
`swarm restack` again to finish. Once restacking succeeds and the task changes
are ready, publish the selected files:

```bash
agentihooks swarm <id> publish \
  --message "Document successor publication" \
  -- docs/pillars/swarm.md docs/pillars/index.md
```

Inspect `branch`, `head`, `worktree` and `gate` in the returned result. For a
restacked successor whose history diverged from the predecessor, expect the
prepared branch and its published head. For a retained fast forward
continuation, expect the retained branch. Use the resulting branch when
opening a pull request into `dev`, then record its URL:

```bash
agentihooks swarm <id> pr <pull-request-url>
```

### Repository configuration and failures

There is no separate branch selection flag or environment variable. Configure
the task's repositories explicitly when it spans repositories; prose names in
the task description do not select them. For an existing task:

```bash
agentihooks ledger --slug <id> --as <agent> \
  task set <task> repositories=/absolute/repo,/absolute/other
agentihooks swarm <id> prepare
```

`prepare` returns a worktree and branch per repository. Run `publish` from the
appropriate returned worktree to select that repository; its recorded branch
is kept separately from the others. From a primary checkout, publication can
resolve a sole task repository, but refuses an ambiguous selection when the
task declares several repositories. Without a `repositories` list, the swarm
repository is the default.

Publication stages only the named files, commits if needed, runs the selected
repository's prepush gate, then pushes. A failure reports a `Next:` command;
fix the reported cause and repeat that command. An already committed head is
graded again without an empty commit. Missing worktrees require `prepare`;
unrelated tracked changes must be dealt with before publication. Tests run in
CI. A push rejected because the remote changed remains a failure to reconcile;
branch selection does not resolve concurrent remote updates.

The successor branch fix was delivered in [pull request 3385](https://github.com/the-cloud-clockwork/agentihooks/pull/3385).
The regression reproduced a push rejected against the predecessor before the
fix ([red proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38723619720));
the corrected publication and continuation cases passed afterward
([green proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38725568065)).

## Plan tracing for promotion fixer tasks

Promotion failures create fixer tasks whose identifiers include the Actions run
number, for example `promo-38842603017-0`. Plan tracing accepts any task
identifier registered in the swarm ledger, matched as a whole token, in the
follow-up text it generates. Free text run numbers are still refused.

### Trace the fix plan

Work as the agent claiming the promotion fixer task. Write `plan.md` in the task
work folder supplied at launch, with one piece per line:

```text
- Repair coverage accounting | scripts/ci_coverage.py | Restore the promotion coverage check
- Add a coverage regression case | tests/test_ci_coverage.py | Prove the reported failure stays fixed
- Add an unrelated feature | scripts/unrelated.py | Extend another service
```

Use the actual areas needed by the fix. Then run:

```bash
agentihooks swarm example-swarm trace-plan
```

The command resolves the agent and claimed task from the session. Before any
classifier call it checks each piece's follow-up text,
`Cut from the plan of task <task id>: <what>`, against the ledger's plain words
rules. Its report names the verdict, kept pieces and cut pieces. If the
classifier cuts the unrelated piece and the plan passes, the filed follow-up
reads:

```text
Cut from the plan of task promo-38842603017-0: Add an unrelated feature
```

Classifier scope and size judgments still apply; accepting the identifier does
not make every plan pass. The command reference above also describes
authorization handling and the separate build and intent gates.

### Configure enforcement

The operator or master selects the gate mode, as described in the
`<gate>-gate` row of the command reference:

```bash
agentihooks swarm example-swarm set trace-plan-gate=deny
agentihooks swarm example-swarm set "trace-plan-gate=log only"
```

The mode governs what follows a failed verdict. `deny` logs each failed plan as
a denial, and a second failed plan with different content sets the task to
blocked, comments the reasons on it and retires the agent. `log only` logs each
failed plan as a would-be denial. `skip` writes no failure row and never blocks
the task, though a refused follow-up still stops the trace; the classifier still runs, the verdict
is still saved, and unchecked verdicts and filed cuts are still logged.

### Plain words limits and refusals

The exception covers a token that exactly equals a task identifier registered
in the swarm ledger. It exempts that token from the commit hash and run or job
identifier checks; other prose rules still apply.

With `promo-38842603017-0` registered and no other promotion task:

| Text | Plain words result |
|---|---|
| `Cut from the plan of task promo-38842603017-0: Add an unrelated feature` | Accepted by the identifier checks. |
| `Run 38842603017 failed` | Refused: a free text run number. |
| `promo-38842603017-1` | Refused: the token is not a registered task identifier. |
| `promo-38842603017-0 Run 38842603017 failed` | Refused: the separate run number remains in the prose. |

A piece whose follow-up would be refused stops the trace in every gate mode,
with `plan line <n>: the ledger would refuse its follow up` and the named
violation. No passing verdict exists for the current `plan.md`; the build gate
parses it again, reports the same refusal and refuses the task's edits under `build-gate=deny` and logs them under `log only` until
the piece is rewritten and a trace passes.
Rewrite the piece's description in plain words; a registered
identifier does not authorize bare run numbers elsewhere in a plan piece or
comment. Put run evidence in the task proof and link the Actions run when
referencing it in ledger prose. If only the generated task token is refused,
confirm the claimed task appears in the swarm ledger under that exact
identifier. Retrying unchanged text does not resolve a validation refusal.

## Deployed host capacity

When `AGENTIHOOKS_DEPLOYMENT` is unset or `local`, host room and the host pressure
alert use the local host. Any other value, including `compose` and `distributed`,
selects deployed behavior: neither check nor the live `autoscale` command reads
the controller's own node for worker capacity.

The Kubernetes runtime supplies host room from the worker namespace's
ResourceQuota and the Pod policy's optional `worker_pool` budget. It uses the
smallest known room from those sources. The pool reading lists every Pod in the
policy namespace without a swarm label selector, counts unfinished Pods and
their CPU and memory requests, and subtracts those requests from the declared
budget. The next worker's selected profile supplies its CPU request (or CPU
limit when no request is set) and memory limit. The smallest remaining Pod,
CPU or memory allowance determines how many more workers fit, with a floor of
zero. This measures declared capacity and requests, not node load or live CPU
and memory utilization; it does not add Kubernetes nodes.

### Configure the worker reading

Use the deployed controller's existing Kubernetes runtime configuration. These
Helm values map to the runtime environment:

| Helm value | Environment variable | Purpose |
|---|---|---|
| `deployment` | `AGENTIHOOKS_DEPLOYMENT` | `distributed` enables Kubernetes worker placement. |
| `controller.workers.apiUrl` | `AGENTIHOOKS_CONTROL_API_URL` | Worker registration API URL; an empty value leaves the worker runtime unconfigured. |
| `controller.workers.podPolicy` or `podPolicyConfigMap` | `AGENTIHOOKS_POD_POLICY_FILE` | Approved Pod policy, including namespace, profiles and optional pool budget. |
| `controller.workers.imageTag` | `AGENTIHOOKS_WORKER_IMAGE_TAG` | Worker image tag, never a digest. |
| `controller.workers.profile` | `AGENTIHOOKS_WORKER_PROFILE` | Profile present in the policy; determines the next worker's requests. |
| `controller.workers.cap` | `AGENTIHOOKS_LAUNCH_CAP` | Positive whole-number launch cap. |
| `controller.workers.projects` | `AGENTIHOOKS_LAUNCH_PROJECTS` | Nonempty project list, rendered as comma-separated project names. |
| `controller.workers.brain` | `AGENTIHOOKS_LAUNCH_BRAIN` | Required brain setting for worker launches. |

For example, merge this fragment into an already valid Pod policy, retaining
its namespace, selected profile and other required fields:

```json
{
  "worker_pool": {
    "pods": 4,
    "cpu_millis": 8000,
    "memory_mib": 12288
  }
}
```

The values declare a four Pod budget with eight CPUs and 12288 MiB of memory.
Use the [Pod policy schema](../swarm-v2/schemas/pod-policy.json) for the complete
policy contract. The chart can read that policy from an existing ConfigMap:

```yaml
deployment: distributed
controller:
  workers:
    podPolicyConfigMap: approved-worker-policy
```

The ConfigMap must contain `pod-policy.json`; retain the deployment's existing
API URL, image tag, profile, cap, projects, brain and service account settings.
The controller needs Kubernetes access to list Pods and ResourceQuotas in the
policy namespace. A missing `worker_pool` still permits a known namespace quota
to supply room. If only one source is readable, that source supplies the room.

### Inspect capacity and off reasons

Run the live command in the deployed controller environment, so it receives the
same deployment flag and worker settings:

```bash
agentihooks swarm example-swarm autoscale
agentihooks swarm example-swarm autoscale --json
agentihooks swarm example-swarm status
```

`autoscale` computes a read-only decision; it neither refreshes account quota nor
writes the stored capacity decision. `status` shows the stored host room from
the last capacity decision. A pool reading reports Pods, CPU and memory requested
against the declared budget and how many more fit. A zero room holds new spawns;
manual scaling still obeys the host room gate.

The host pressure alert is always off in deployed mode, even when worker room is
available. After the pressure check records that state, `status` prints:

```text
host pressure alert off: a deployed controller's own node says nothing about worker capacity; the host room line shows the worker pool reading or why it is off
```

If the Kubernetes runtime is absent, autoscale instead reports:

```text
host room None: host room off: the Kubernetes runtime is not configured on this deployed controller, so the worker pool cannot be read and spawns pass
```

Refused worker settings append the settings refusal to that reason. If no worker
reading is wired into the controller tick, its host room is also off with an
explicit reason. If neither namespace quota nor pool budget yields known room,
the reason names the missing budget or failed reading. Unknown room permits
spawns through the host gate; quota and launch controls still apply. Check the
runtime settings, policy profile, declared pool and namespace API permissions
when room is unknown. Do not use the controller node's load to infer worker room.

## Context recycle

When its context reaches `AGENTIHOOKS_COMPACT_LIMIT` thousand tokens (default 600), a swarm agent receives a handoff preparation directive. Its deadline is twenty five minutes later or the hard gate, whichever comes first. The hard gate sits `AGENTIHOOKS_HANDOFF_MARGIN`
thousand tokens above the limit (default 50, so 650 by default); there a hook tells it to write a handoff document and a recap and run
`agentihooks swarm <id> handoff <doc> --recap <recap>`, then stop. From that point PreToolUse denies every tool call except
reading files (in the shell too: cat, head, tail, ls, wc, grep, git status, log, diff, show), writing or
editing files under `~/scratchpad`, `agentihooks swarm <id> handoff <doc> [--recap <recap>]`,
`agentihooks swarm <id> learned <text> [--maturity <level>]` and the `agentihooks ledger` comment, say, leave and ack commands, each as one command with no chaining; the deny
reason repeats the handoff command. The task stays claimed, and the next tick starts a
successor on the same task with the document in its opening prompt. A task that already has a pull request
keeps it and stays in `pr` state under its successor. `agentihooks swarm <id> set compact-limit=N`
sets the limit for one swarm's next agents; 0 keeps the default.

Every agent's opening prompt carries its seat's priming chain, in order: the handoff document, the swarm
culture, the latest recap, the learned notes, then up to three older recaps (the count of any further ones is
stated). A missing piece is named, not skipped; a seat with no history and a swarm with no culture get a prompt
that says so. Recaps and learned notes live in Redis under the seat and are append-only.

Each learned note has a maturity: `data`, `note`, `insight` or `canon`, ranked in that order; notes written
before maturity existed read as `note`. The priming chain lists canon first, then insights, then notes, and
shows data only as a count. `promote` only raises a note, never lowers it, and records each step with its
reason. The culture is one text per swarm, kept in Redis outside the swarm's own keys, so like seat memory it
survives `remove` and a swarm created again under the same id reads it.

## One task per agent life

An agent is spawned for one task, told that task in its opening prompt, and ends when the task is done or
blocked. The prompt walks it through a fixed order: open an issue where the repo has issues, create a worktree, red test then green,
pull request into `dev`, merge on green, then `done`. An agent that cannot finish pushes a draft pull request
and calls `block`. A finished agent is retired on the next tick and its pane closed.

That order is for a task of kind `code` (the default) or `ci`. A task's kind (`ledger task add --kind`)
picks its prompt and the proof `done` must carry, and the ledger refuses `done` without it:

| Kind | Ends with | `done` flags |
|---|---|---|
| `code`, `ci` | a merged pull request | `--pr URL` |
| `ops`, `tune` | a verified system state | `--command C --output O` |
| `troubleshoot` | the root cause shown by evidence, and a fix or a proposed follow-up | `--root-cause R --evidence E`, `--fix URL` or `--filed FOLLOWUP` |
| `research` | a written finding | `--finding URL` |

For `code` and `ci`, `done` reads the pull request (`--pr`, else the task's recorded one) with `gh pr view`
and refuses unless it is merged, or when GitHub cannot be read. The tick backs this up: a `code` or `ci` task
an `eng` or `ci` agent marked done in the last day whose pull request is not merged is reopened, with a task
comment naming the pull request's state. A done from the master or the operator stands, and a pull request
seen merged is not read again.

`--must`, `--check` and `--judge` on `task add` store a proof contract (what must be true, how it is
checked, who judges it) that the agent's prompt carries.

### Shared areas in the opening prompt

When the tick claims a task, it compares the task's territory with the territory of every other task in `claimed`
or `pr` state. Two entries share an area when they are equal or one is a folder holding the other; surrounding
spaces, a leading `./` and trailing slashes are ignored, and the narrower entry is the one named. Sharing an area
never blocks a claim; it moves the task back within its rank. The launch order is queue rank (or the dispatcher's
band), then tasks without an earlier failed launch, then resumed work, then tasks clear of every held territory, then
the S exception, critical path and ledger order described under [Queue rank](#queue-rank). With dispatch active,
the dispatcher's queue position replaces those last three within a band. An `urgent` task that shares an area still
claims ahead of a clear `normal` task.

The opening prompt then carries a header line once, followed by one line per running task that shares an area,
after the task, contract, workspace and group lines. Several shared areas are joined with `and`:

```text
Running tasks share areas with yours. Coordinate with each claimant through the inbox (agentihooks msg send <claimant> "<text>") and merge dev only on a real conflict; where the repo has a merge queue, it retests your green head against the newest dev:
Task t1, claimed by engineer@a1b2c3-0001, shares hooks.
Task t2, claimed by engineer@a1b2c3-0002, shares docs/a.md and scripts/swarm/tick.py.
```

A task that shares nothing gets no such lines. The agent coordinates with each claimant through the inbox and
merges `dev` into its branch only on a real conflict. The engineer skill adds one case the prompt line leaves out:
where the repo has a merge queue, a green head is queued as it stands, because the merge group tests it on top of
the newest `dev`, and the agent also merges `dev` when GitHub refuses the enqueue.

### Split tasks across pull requests

A `code` or `ci` task can declare several plan line ranges for separate pull requests while keeping one task and its completion proof. Declare the split **before recording any pull request**. The ranges must be ordered, must not overlap and, when the task has `plan_lines`, must cover that range completely.

For example, a task covering plan lines 1 through 20 can declare two halves:

```bash
agentihooks ledger --slug <id> --as <agent> \
  task split <task> 1-10 11-20
```

This stores the ranges in `split`. Each half still follows the normal review, checks and merge workflow. Record the first half's pull request on the same task with:

```bash
agentihooks swarm <id> pr <first-pull-request-url> --half 1
```

The task's `pr_url` names the active pull request; `split_pulls` retains the pull requests for its halves. At a worker's next Stop check, when the active pull request is merged, belongs to `split_pulls` and fewer pull requests are recorded than declared halves, the stop guard clears `pr_url`. It moves a task still in `pr` back to `claimed`, preserving both `split` and `split_pulls`. This update is guarded on the task still being in `pr`.

Continue the remaining half and record its pull request with `--half 2`. Half numbers start at 1 and must be recorded in order; a new pull request for a split task requires `--half`. Clearing the old link does not complete the task or permit an idle stop: with no active pull request, the worker must continue work or declare a valid wait. For example, while waiting for an actual dependency:

```bash
agentihooks swarm <id> wait --on task <dependency-task>
```

After every declared half has a recorded, merged pull request, close the task with the final pull request:

```bash
agentihooks swarm <id> done --pr <final-pull-request-url>
```

When the worker keeps the task, the merged final half still requires `done`. Completion refuses a missing half or any recorded pull request that is open, closed without merging or unreadable on GitHub. There is no separate configuration switch for the between-halves reset; the declared split and recorded merge state determine it.

#### Ready handoffs for split tasks

A reviewed split task can release its seat with `ready` while checks and merges finish. The ready merge pass applies the same split completion guard as `done`: every declared half needs a pull request, and every registered pull request must have merged. A merge of the watched pull request alone cannot complete the task.

For a two half task declared above, record each half in order as its pull request becomes available:

```bash
agentihooks swarm <id> pr <first-pull-request-url> --half 1
agentihooks swarm <id> pr <second-pull-request-url> --half 2
```

After the work is complete and review is closed, prepare a Handoff v2 document using the installed `handoff` skill, leave the ledger crew and release the seat:

```bash
agentihooks ledger --slug <id> --as <agent> leave
agentihooks swarm <id> ready <handoff-document>
```

The normal `ready` requirements still apply: a code or ci task, delegate or full autonomy, a recorded open pull request, and a clean worktree whose head matches that pull request. No additional environment variable enables split completion; `split` and `split_pulls` supply the configuration. Register all halves before releasing completed work. `ready` does not create a missing half or its pull request.

| Merge observation | Task and cleanup behavior |
|---|---|
| First half merged; another registered half has not merged | The task stays `pr`, with `awaiting` set to `merge` and its active `pr_url` retained, normally naming the remaining half. The ready record, handoff and worktree remain. The released worker does not need to hold its seat. |
| A declared half has no recorded pull request | Completion stays blocked even if the watched pull request merged. Finish and register the missing half; waiting alone cannot supply it. |
| Another half is open, closed without merging or unreadable on GitHub | The split guard refuses completion. Check that half's URL and GitHub state; repair unfinished work or restore the read before expecting completion. |
| Every declared half has a recorded, merged pull request | The merge pass completes the task and its group members, clears `awaiting`, removes the ready record and handoff, and runs normal worktree cleanup. `split_pulls` remains as the history. No worker `done` command is needed. |

Inspect the ledger's task and each registered pull request when completion appears stuck. The split guard does not replace normal ready recovery: red checks, closure or queue ejection of the watched pull request follow the `ready` command's reopening rules above. A different half failing the split guard prevents completion without discarding the handoff.

Delivered by [pull request 3290](https://github.com/the-cloud-clockwork/agentihooks/pull/3290). Its regression proof reproduced premature closure before the fix ([red](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38646452965)); the continuation proof passed 114 focused tests, including ready and split completion ([green](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38679415151)).

#### Intent grading of each half

The [intent check](#commands) judges each half as its own pull request. When `swarm pr` records a half, the intent prompt names that half and its plan lines, notes which plan lines the other halves carry, and reads only that half's plan slice. Each recorded half keeps its own verdict, head and input fingerprint, so a `gh pr merge` of one half reads only that half's verdict.

Matching starts once the first half is recorded with `swarm pr <url> --half 1`; before that the task has no intent record, and a merge is not checked. From then on the `intent` gate matches every `gh pr merge` of the task to the half it belongs to before it reads a verdict:

- A merge that names a pull request URL is matched to that recorded half. A merge that names a bare number is matched on the number alone, against the recorded halves of every repository; when more than one recorded half has that number, the merge is refused with `Intent requires a pull request registered on this task.`
- A merge that names only a branch, or names nothing and so merges the current branch, is resolved with `gh pr view [<branch>] --json url --jq .url` in the command's working folder. A `-R` or `--repo` option is passed through, so a half in another repository resolves there. The resolved URL is then graded like a named merge.
- A merge that resolves to no pull request, because the view fails, times out after 20 seconds or prints no pull request URL, is refused:

  ```text
  task <task> is split: name the pull request url in the merge so intent matches it to its judged half.
  ```

- A resolved or named pull request that is not one of the task's recorded halves is refused with `Intent requires a pull request registered on this task.`, or, before the task's halves are registered for intent, with `intent for task <task> was judged on <url>, not this pull request. Record this half first: agentihooks swarm <id> pr <pull request url> --half <n>, then wait for its verdict.` After the first message, record the half the same way.
- A half with no verdict yet, or one judged on another head or another task contract, is refused with `Intent must be checked on this pull request head and contract before it can merge.` Wait for the tick to judge the current head. A GitHub error or timeout while reading the half's current head gives the same refusal.

For example, from the worktree of a split task's second half, both of these merge commands are graded against half 2:

```bash
gh pr merge --squash
gh pr merge engineer-example-2 --squash --repo the-cloud-clockwork/agentihooks
```

The lookup runs only for a task whose intent record carries a split; a merge of an unsplit task reads nothing from GitHub at this step. Name the pull request URL in the merge to skip the lookup. `swarm done --pr <url>` and `ready` are not matched to a half: they read the task's latest verdict, and `done` verifies separately that every recorded half merged.

A split cannot change which pull request a verdict belongs to. Once the task records any pull request, `task split` is refused, as the table below lists, so a half's verdict always grades the plan lines it was judged on.

#### Split refusals and stop messages

| Message or condition | Meaning and action |
|---|---|
| `the task has a pull request outside the split` | A pull request was already recorded before the split. The split is refused without changing the task; declare splits before recording the first pull request. |
| `a half has a pull request` | A split half already has a recorded pull request, so the split cannot be rewritten. Keep the declared ranges and history. |
| No open pull request and no wait, after a half merged | The stop guard has moved past the merged half. Continue the next half or declare the real condition being awaited. |
| A half has no pull request yet | Completion is premature. Record and merge the remaining half before `done`. |
| The final pull request merged; close the task now | All halves have recorded pull requests. Run `done`; it verifies that each one merged. |
| `task <task> is split: name the pull request url in the merge so intent matches it to its judged half.` | The `intent` gate could not resolve the branch or current branch of a `gh pr merge` to a pull request. Rerun the merge with the half's pull request URL. |
| `Intent requires a pull request registered on this task.` | The merged pull request is not a single recorded half: it is unrecorded, or its bare number matches more than one half. Record it with `swarm pr <url> --half <n>` and wait for its verdict. |
| `intent for task <task> was judged on <url>, not this pull request. Record this half first: agentihooks swarm <id> pr <pull request url> --half <n>, then wait for its verdict.` | The merged pull request is not the half the verdict was judged on. Follow the command in the message. |
| `Intent must be checked on this pull request head and contract before it can merge.` | The half has no verdict for its current head or task contract, or its head could not be read. Wait for the tick to judge it, then merge again. |

## The minute tick

A systemd user timer (`agentihooks-swarm.timer`) runs `agentihooks swarm tick` every minute. `start` installs
and enables it. Every swarm ticks in its own thread; while one swarm's tick runs long, a quicker swarm ticks
again each minute until every swarm's first tick of the pass ends; no extra tick starts after seven minutes.
An outage holds the affected idle and delivery decisions until recovery (see
[Outage holds and recovery](#outage-holds-and-recovery)). Each tick, per swarm:

1. Retire agents that finished, and pass on the messages they left (see [Safe retire](#safe-retire)).
2. Free the tasks of agents whose pane is gone, or that stayed idle for 20 ticks. A stale agent is nudged through its inbox
   (see Stale nudge).
3. Reopen claimed tasks that have no agent.
4. Spawn the master if none is online, or retire it once a stopping swarm has no worker left. After the down window, force a launch and promote an engineer when it fails (see [Master outage and promotion](#master-outage-and-promotion)).
5. While `running`, spawn agents up to the caps, one per claimable task, as long as a Claude account has room
   under its session cap. Claimable tasks are taken highest [queue rank](#queue-rank) first. Sessions go to the
   lanes in rounds: a lane with fewer live agents spawns before one holding more, ties in eng, ci, plan order, so
   every lane with ready work gets an agent before any lane takes a second.
6. Mark the swarm `stopped` when no agent is left, or `drained` when only the master is and nothing remains to do.
7. Post inbox replies to the operator on the page chat.
8. Run the [ledger event pass](#ledger-event-pass): agent writes and time rules become inbox items.
9. Tick a phase whose tasks are all done, and reopen a ticked phase when a task that is not done lands in it.
   Each change leaves a status comment from `swarm` on the phase and an information item for the master. A phase
   with no task, or out of scope, is left to the master.
10. Send each new health finding to the master with the `verdict` command to judge it.
11. Wake idle panes holding unread items, except a Codex pane whose push pump owns its inbox, and climb the wake
    ladder for items nobody reads.
12. Write the automatic snapshot when it is due.

The health pass reports `merge stall` when the dev merge queue holds entries and no pull request has merged
into dev for sixty minutes. Its evidence names the queue length, minutes since the latest dev merge and the
most common failed job among the ten most recent merge queue workflow runs. The master receives a notice
on detection and every hour while it persists, even after giving the finding a verdict. This signal only
reports the stall; it imposes no enforcement and changes nothing in the queue. GitHub observations are cached
for five minutes. A failed observation retains the previous evidence and delays further notices until a fresh
observation succeeds. An empty queue or a new dev merge resets the notice interval.

Each tick renews the controller lease for three ticks. A tick is 60 seconds unless
`AGENTIHOOKS_CONTROLLER_TICK_SECONDS` sets another interval of at least one second. Set it only on the controller
of an `agentihooks controller run` install; `swarm tick` refuses to run with it set, because the host timer ticks
every 60 seconds. Redis server time determines expiry. The owner comes
from `SWARM_HIVE_ID`, falling back to the hostname. Ledger and spawn writes carry the epoch; stale controllers
are refused. The tick lock prevents two ticks in the same epoch and lets a higher epoch take over.

### Stalled activity findings

A `stalled` health finding means a pane reports `working` while its recorded
tool activity has been quiet for at least `AGENTIHOOKS_HEALTH_STALLED_MINUTES`
(default **10**). A pane that is not working, or an agent with no recorded
tool timestamp, does not produce this finding. Inspect the finding through:

```bash
agentihooks swarm <slug> status --json
```

Compare the finding with recent tool calls and the agent's progress. Inbox
messages and task changes can show that work continued, but successful commands
alone do not establish that the hook retained the identity needed to record
activity. A finding is evidence to investigate; the master or operator judges
it with `verdict`.

#### Wrapper loss and swarm identity

A launched Claude agent can keep running after its agentihooks wrapper exits.
Its process is then reparented, so the hook's parent walk no longer reaches the
original launcher. Before the correction delivered in
[pull request 3906](https://github.com/the-cloud-clockwork/agentihooks/pull/3906),
that condition caused hooks to treat the agent as nested and drop its swarm
identity. Tool calls continued while activity recording returned early, leaving
the stall clock frozen and producing false findings.

The hook now checks the launcher's terminal session when its parent walk sees
one agent but misses the launcher. The launched agent keeps its identity when
it is the only recognized agent in that session. This requires no manual
identity repair or new setting. A recognized agent in another terminal session,
a session containing another agent, or an agent nested inside an orphaned
agent remains isolated from the outer swarm identity. Consecutive processes
with the same agent command name are treated as one wrapper chain.

The check recognizes Claude and Codex process names. This correction does not
establish Copilot orphan recovery or explain why a wrapper exited.

#### Configure the threshold

Set the threshold in the environment used by the health pass. For a manually
invoked tick, this example raises it to twenty minutes for that invocation:

```bash
AGENTIHOOKS_HEALTH_STALLED_MINUTES=20 agentihooks swarm tick
```

For a timer or controller, supply the variable through that service's managed
environment so later passes use the same value. Setting it only in a worker
shell does not configure a separately running health pass. Raising the threshold
changes when the finding appears; it does not repair a frozen activity record.

#### Investigate an active agent reported as stalled

1. Read `status --json` and compare the reported inactivity with recent tool
   calls, progress and task changes. Successful hook execution with a frozen
   activity clock matches the symptom investigated in
   [issue 3899](https://github.com/the-cloud-clockwork/agentihooks/issues/3899).
2. Confirm the running hook installation includes the correction. Use read-only
   process diagnostics to compare the agent's parent and terminal session with
   the launcher when investigating wrapper loss; do not dump its environment.
3. Observe a subsequent tool call and the next health pass. If inactivity still
   grows, retain the hook and process evidence for investigation. Do not remove
   `AGENTIHOOKS_SWARM_LAUNCHER` to force identity acceptance: it protects nested
   sessions from inheriting the outer task.
4. The master or operator records the evidence-based verdict. For example,
   substitute the finding identifier returned by status:

   ```bash
   agentihooks swarm <slug> verdict <finding> false-positive --note 'Recent tool calls continued while activity recording was frozen'
   ```

A verdict suppresses the finding under the existing cooldown rules; it does
not update the activity clock. See the `verdict` command above for recurrence
behavior and [Outage holds and recovery](#outage-holds-and-recovery) for provider
or Redis outages, which have their own hold and resume behavior.

### Ledger event pass

Each swarm keeps a cursor on the ledger's event list. The tick reads the events written since it and sends one
inbox item from `swarm` per agent event; writes by the operator or by a master are skipped. The first pass after
the cursor is created (a new swarm, or Redis lost) only sets it. Every item is sent once, so replaying the same
ledger sends nothing.

| Event or rule | Goes to |
|---|---|
| An agent adds a follow up | The master, asked to turn it into a task, close it with a status or flag it for the operator |
| An agent adds a question | The master, asked to answer it or raise it to the operator |
| An agent blocks a task | The master, asked to read its last comment and unblock, rewrite or raise it |
| An agent closes a task as done | The master, with the pull request and proof to check |
| A follow up not added by the operator, still open 15 minutes after it was added | The master again; at 30 minutes the operator's Priorities |
| A task still in `pr` 10 minutes after its pull request merged | Its engineer, told to run `done`; at 20 minutes the master |
| A task's pull request closed without merging | Its engineer |
| A task's pull request with red checks and no push for 20 minutes | Its engineer, once per push |

A follow up already closed or flagged for the operator is not raised. Pull request state comes from `gh pr view`;
a pull request `gh` cannot read is skipped until a later tick. An engineer that is gone falls back to the master.

These items ride the same wake ladder as any other (see [Talking to the swarm](#talking-to-the-swarm)). Before
it wakes anyone, the tick closes an event item whose follow up or question was already decided on the ledger
(follow up closed or flagged, question answered or out of scope), so the master never handles a decision twice.
A task item stays until the master closes it, so a done task's proof check always reaches the master.

### Priorities clear themselves

Each tick first sweeps the priorities agents raised: one whose item is done, out of scope or gone, or a task whose
pull request merged, is cleared. Then it reads the ledger events since its own `priority-cursor`. Each comment,
comment edit, answer or chat line by the operator or an agent on an item that carries a priority (a chat line
counts for an item whose id it names) asks the classifier one yes or no question, with purpose
`priority-resolve`: does this write resolve what the priority asks. A yes clears the priority, marks a follow up
done, records an operator comment on a question as its answer and comments the classifier's verdict on the item.
A no, or no classifier answer, leaves it. A priority that waits on an operator decision (a question, a task
awaiting merge approval, a follow up flagged for the operator, a plan escalated to him) counts only the operator's
writes, a master `relay` carrying his verified words among them: an agent's comment, answer or chat line on it is
never judged, even one that asks him for the decision. Every automatic clear is a `priority cleared` event by `swarm` with a
`reason` in the ledger history.

### Stale nudge

An agent holding a task is stale when its session heartbeat is over ten minutes old and no named wait holds, or
when its pane shows a provider error while its heartbeat is not working. The tick sends it one inbox item per ten
minutes of silence, with an id built from the agent, its last activity and the window, so repeated ticks send it
once; nothing is typed into its pane. The inbox channel delivers it to Claude and the Codex push pump to Codex. A
nudge the pump delivers within two minutes of its sending interrupts a turn still running and starts a new app server
turn; a later one is delivered like any message. An agent on a long command declares a wait first.
The master, an agent the operator is typing to, a swarm with an open outage and an agent in provider recovery get
no nudge.

### Safe retire

The tick retires an agent for idleness only after 20 idle ticks, and a tick counts as idle only when all three
say idle:

- its herdr pane, read by its pane id, never by its name, reads idle;
- its session heartbeat does not say `working` (hooks write `working` on each prompt and tool call and `idle` at
  Stop; a `working` beat older than 20 minutes no longer counts);
- no wait it declared with `agentihooks swarm <id> wait` still holds. A declared wait stops idle counting until its
  end time, so an agent waiting on checks, a deploy or a reply is neither nudged nor retired.

When an agent leaves, retired, stalled, lost or exited on its own, its open inbox items are settled. If its task
goes on (a handoff, or a task reopened for a successor), each item moves to its seat for the next occupant. If
nobody takes the task up (it is done or blocked, for example), each item is withdrawn and its sender gets an
item naming the agent and the message; a sender that has itself left is told through the master. Agents that
exited between ticks are settled at the start of the next tick.

## Queue rank

Every task carries a queue rank: `urgent`, `high`, `normal` or `low`. A task without one, every task written
before ranks existed included, counts as `normal`. `next` is accepted as an alias and stores `urgent`, the top
rank; it puts a task first in the queue until someone changes its rank again.

The tick orders claimable tasks highest rank first. Within a rank, a task with a longer critical path goes first:
the longest chain of open tasks waiting on it. One exception: an S task that unblocks another open task (one whose
other dependencies are all done), or is the last task of its phase not yet done, goes ahead of the rest of its rank.
Phase order is never used, and ledger order breaks the remaining ties. Capacity placement reads the same order. The
tick then applies the usual
rules: lane, phase, open dependencies and live claims still decide whether a task is claimable, so an
urgent task never skips a dependency that is not done and never takes a task another agent holds. A territory
another agent holds moves a task back within its rank without blocking it (see
[Shared areas in the opening prompt](#shared-areas-in-the-opening-prompt)).
Among open tasks whose territories overlap, the higher rank claims first. The rank is read from the ledger on every
pass, so a change applies on the next tick.

At `delegate` and `full` autonomy the dispatcher guards the critical chain: when a task on the longest open
dependency chain among the work no freeze or focus holds merges, its claimable successor takes the next free seat in its lane ahead of every rank, resumed
work and territory order, in the tick that reads the merge. The dispatcher then comments on that task with the
seconds from the merge to the claim and logs a `chain-guard` row.

Set it with `agentihooks ledger task add ... --rank R` or `task set ID rank=R`, or from the select on each task row
of the ledger page, which lists open tasks in claim order. The master, a planner and the operator may set it; an
engineer or CI agent is refused and proposes the change as a follow up. The queue rank is separate from the
Priorities panel, which holds decisions waiting on the operator.

A task filed under the wrong phase moves with `task set ID phase=P`. The master and a planner may move it; an
engineer or CI agent is refused, and so is a phase the ledger does not hold. The move is stamped and recorded as a
`task moved` event. Each intent verdict records the phase it was judged under, and a verdict from another phase is
judged again on the next tick against the task's current phase.

## Freeze and focus

A freeze or a focus holds open tasks back from a claim; it never stops work already claimed. Only a task in the
`open` state is held, so an agent holding a task under a freeze or outside a focus finishes it, its pull request
merges and the task closes as usual. Write them with `agentihooks swarm <id> freeze TARGET`, `focus TARGET` and
`unfreeze TARGET` (see [Commands](#commands) for targets and who may write them), or from the ledger page.

- **Freeze** holds the target and everything under it. A freeze on a phase holds every open task in that phase,
  its slices included; a freeze on `lane:ci` or `kind:research` holds every open task of that lane or kind. Rank
  does not matter: a frozen `urgent` task stays held.
- **Focus** holds everything outside the target. While any focus record exists, every open task not under a
  focused node, and not matching a focused selector, is held. Several focus records add up: a task under any of
  them may be claimed. A focus on a single task leaves only that task and the exempt tasks below claimable.
- **Urgent tasks are exempt from a focus.** A task ranked `urgent` (see [Queue rank](#queue-rank)) stays claimable
  outside every focus, so a fix the operator raises to `urgent` is never held by a focus elsewhere. In a Doctor
  swarm, tasks in the Doctor's fix phase are exempt too. Neither exemption applies to a freeze.
- Freeze and focus combine: a task must pass both, so a frozen task inside a focus stays held.

Both records are cleared with `unfreeze TARGET`; there is no separate unfocus command. `unfreeze` on a node clears
only the freeze and focus records on that node, keeping every record on the nodes under it, and on a selector clears
the records on that selector. The two release work differently. Unfreezing a frozen phase releases the open tasks under that phase
from its freeze; a focus that does not cover them keeps them held. Clearing the last focus releases every task the focus held across the whole ledger at once, apart from
tasks still under a freeze. A held task that becomes claimable starts its claim wait at that unfreeze or focus.

A task that depends on a held task is not claimable either, because its dependency is not done; the page's time left
counts it with the held work.

### Seeing held tasks

`agentihooks swarm <id> status` prints one line per active record, after the phase lines:

```
focus  phases/p92  by operator  at 2026-10-15T14:02Z  ship the docs first
freeze  lane:ci  by operator  at 2026-10-15T14:05Z
```

Each line names the verb, the target, who wrote it, when (UTC) and its reason when one was given. Status does not
list freeze or focus holds task by task; a phase line's `holds` list names open tasks its phase does not admit yet
(the phase waits on another phase, is still to plan, planning or in review), not tasks held by a freeze or a focus.
A task is held when it is `open` and a freeze covers it, or a focus exists that does not cover it and the task is
neither `urgent` nor in a Doctor swarm's fix phase. In a Doctor swarm, a focus on the watched ledger also holds its
tasks that are neither `urgent` nor in the fix phase. When no task is claimable and no lane agent is left, the tick
marks the swarm drained and posts a ledger notice. It names how many open tasks are held and by which records, for
example `The swarm has no task it may start: 3 open tasks are held by the focus on phase Documentation agents`, and
says only `The swarm has no task left to start` when none is held. The ledger page's time left leaves held tasks out
and says so: `with 3 frozen tasks holding 90m of work left out`.

## Redis

Redis holds the swarm's runtime state, and the swarm refuses to run without it:

- the config of each swarm and the index of swarm ids;
- one claim per task, with a 10 minute lease that each tick renews for live agents, so a task has one owner;
- the agent registry;
- the tick lock.

The default is `redis://127.0.0.1:6379/0`; set `AGENTIHOOKS_SWARM_REDIS_URL` to use another. Task content
(title, lane, state, links) lives in the ledger, not in Redis.

## Talking to the swarm

- Operator: the chat on the ledger page, or `agentihooks swarm <id> send-message "<text>"` for every live agent's
  inbox. A page chat line with no `@` address, or one for an agent not in the swarm, goes to the master's seat. The
  master answers with `agentihooks msg reply <message> "<text>"` and posts its own updates with
  `agentihooks swarm <id> say --to operator "<text>"`.
- Agents: `agentihooks swarm <id> say "<text>"`, optionally `--to <agent name>`, `master`, `eng` or `ci`.

Every addressed message goes through the agent inbox, with one item per recipient, sent by the real author;
an unaddressed agent line only shows on the page. Items pass from pending to delivered, then confirmed by
the receiver; delivery alone leaves them eligible for redelivery. The receiver gets the item through its
inbox channel, managed Codex thread or next tool call. The tick prompts an idle worker pane only when its
transport does not own push delivery; unread items climb to the
master, then to the operator. A reply to the operator is posted on the page chat and its item closed. The
page keeps the whole conversation.

Every operator write on the page reaches the inbox the moment the ledger server applies it: a comment or reply,
an answer, a note, a check or uncheck, a chat line. A write on a task goes to the agent that claimed it, a chat
line to its addressee, everything else to the master's seat; an addressee that is gone falls back to the master.
A chat line sent To swarm (`@swarm`) is one item for every live agent, master included, with the same text and
images; the page shows it once, marked as sent to the whole swarm. Only the master's item is work it must answer;
every other agent's copy is information only and asks for no reply. With no live master the item waits at the
master seat for the next one, and the tick tells the page chat that the master is down.
A stopped swarm whose master seat holds a pending item is paused, so the tick starts its master and no engineer.
The ledger hook, the ledger watch and the inbox share one seen mark per agent and write. Stable item ids keep
relay retries from creating a second item; an unconfirmed delivery may still be shown again.

### Transport capabilities

`agentihooks swarm <id> status` and the ledger page Agents panel show, for every
agent, two things its transport can do. Both come from the harness and the Codex
inbox transport recorded on the agent when it launched; neither is a live probe.

| Harness and transport | Push inbox | Native terminal |
|---|---|---|
| Claude | yes: the inbox channel pushes each item into the session | no |
| Codex, `app_server` | yes: the Codex push pump delivers each item to the managed thread | yes: herdr opens the managed thread as a viewer |
| Codex, `legacy` | no: the tick types a wake into an idle worker pane; the Codex master, whose pane is never typed into, runs `agentihooks swarm <id> wait --inbox` when no work remains | no |
| Any other or no harness | no | no |

- **Push inbox** means the execution receives inbox items without a typed wake.
  An agent without it relies on the tick's wake prompt and its own
  `agentihooks msg inbox` read.
- **Native terminal** means a terminal can attach to the running execution's own
  thread; closing that terminal leaves the execution running. See
  [Managed Codex terminals](../reference/codex-managed-terminal.md).

A Codex agent keeps the transport it launched with. After
`agentihooks swarm <id> set codex-inbox-transport=app_server`, running Codex agents
still show `legacy` capabilities until they are replaced; agents launched next
show the new ones.

The tab separated agent table in status text has `Push inbox` and
`Native terminal` columns, `yes` or `no`, before `Model source`. Columns are
aligned and shortened here:

```text
Agent                 Lane    Harness  ...  Push inbox  Native terminal  ...
master@ed7448-0002    master  claude   ...  yes         no               ...
engineer@ed7448-0003  eng     claude   ...  yes         no               ...
engineer@ed7448-0004  eng     codex    ...  yes         yes              ...
ci@ed7448-0002        ci      codex    ...  no          no               ...
```

`status --json` carries the same pair on each agent:

```json
{"name": "engineer@ed7448-0004", "harness": "codex", "transport": "app_server",
 "capabilities": {"push_inbox": true, "native_terminal_attach": true}}
```

The Agents panel shows them in its `transport` column, after `model`: `push` or
`no push`, and `native terminal` or `no native terminal`, each with an on or off
dot. A status payload without capabilities, such as one from a ledger server
older than this column, shows `—`. Remote swarms, whose panel adds execution and
terminal columns, keep the transport column in the same place.

To list the agents without push inbox, which rely on a typed wake:

```bash
agentihooks swarm <id> status --json \
  | jq -r '.agents[] | select(.capabilities.push_inbox | not) | .name'
```

### Delivered then confirmed

The inbox keeps an item and its history until it is closed. A hook or channel claims a pending item and marks
it delivered. A later hook confirms items delivered to that receiver before the hook began: the session has
continued past delivery. `agentihooks msg read <id>` records receipt as read; `msg reply` or `msg close`
settles the work. Confirmation proves receipt, and leaves the agent responsible for answering or closing it.

Mark pure information with `--fyi` at send time: it closes as `done: information received` on explicit read
or confirmed receipt. Status reports that request an action, proof checks and unresolved follow ups remain
work items. After handling confirmed work, explicitly reply or close it with an outcome. The hook reminds
the receiver of read or confirmed work still open after `AGENTIHOOKS_INBOX_RETRY_WINDOW_S`, at most once
per window. Reminders preserve the item's state and history; unresolved work stays open.

If delivery remains unconfirmed for `AGENTIHOOKS_INBOX_REDELIVER_S` (default 300 seconds), the inbox returns
the same item to pending. Whichever transport claims next can deliver it: the channel, hook or wake path.
A failed channel write returns its unsent items to pending immediately. The redelivery window is separate
from `AGENTIHOOKS_INBOX_RETRY_WINDOW_S` (default 300 seconds), which controls idle wakes and escalation.
Agents should read the item's current state before repeating work after redelivery.

### Local outbox and limits

When Redis is unavailable, inbox operations queue in `inbox-outbox.jsonl` under the AgentiHooks home. Records
are checksummed and fsynced before the sender reports them queued. A queued result means accepted locally;
the receiver sees it after replay. While records wait, new operations queue behind them to preserve order.
Each inbox connection and swarm tick attempts replay. Replay stops on a Redis failure and retains that
record and everything after it; stable item ids make a retry safe when Redis accepted a send before its
response was lost.

| Setting | Default | Behaviour |
|---|---|---|
| `AGENTIHOOKS_INBOX_OUTBOX_MAX_BYTES` | 1048576 bytes (1 MiB) | Refuse an operation that would exceed the limit, with an error; retain everything already queued. |
| `AGENTIHOOKS_INBOX_OUTBOX_WARN_BYTES` | 786432 bytes (768 KiB) | Warn when the queued bytes reach this threshold. |
| `AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS` | 3 | Set aside a record after this many replays that fail with an unexpected error. |

Corrupt records and operations refused during replay are preserved in `inbox-outbox.skipped.jsonl`, with
the reason reported; later valid records can continue. A record whose replay fails with an unexpected error
stops its lane and is retried on the next replay; its failures are counted in `inbox-outbox.attempts.json`,
and at the attempt limit it is set aside the same way so the records behind it replay.
`agentihooks swarm <id> status` prints the queued, retrying and set aside counts while any is nonzero, and
`status --json` carries them under `outbox`. Inspect and resolve those refusals rather than
treating them as delivered. The outbox depends on the local disk surviving and having space, and its limit
is a refusal boundary. Restoring Redis lets replay free space; a send refused before queuing must be retried.

Operator ledger writes and alerts retain their oldest unrelayed revision in `ledger-unrelayed.json` when
neither Redis nor the outbox accepts them. The next write to that ledger retries the intervening events;
the ledger watch also retries every five passes, about ten seconds at its default interval. Stable ids
prevent duplicate inbox items. Ledger replay is limited to its retained history of 2000 events: writes
older than that history cannot be reconstructed by replay. The ledger records a notice naming the lost
revision range for the operator; the watcher and tick also report trimmed history gaps.

### Outage holds and recovery

The tick records Redis, ledger and provider outages locally under the swarm home, so the evidence remains
available when Redis is down. `agentihooks swarm <id> status` shows open outages and their evidence. A failed
Redis check ends the pass cleanly; a failed ledger probe holds decisions that need the ledger. Provider
error signatures in an agent pane open a provider outage. Recovery requires an affirmative service check:
for a provider, one minimal request per affected provider account on a backoff from one to ten minutes.
Each account stops probing after its first success; the provider record closes once every affected account
has succeeded. Pane text alone cannot prove recovery.

While a swarm outage is open, or an agent's own pane shows a provider error, its idle count stays where it
was. The tick sends no idle nudge, retires no affected worker or idle master, and reclaims no affected task.
The wake ladder pauses wakes and escalations during the outage, then subtracts outage time from its
deadlines so it continues from where it stopped. Idle and stale claim findings for held agents carry the
outage evidence and send no finding notice to the master. Once the hold lifts, idle counting resumes from
the held count.

When service returns, the tick sends one marked resume prompt to each stalled agent that still owns its
task. The prompt tells it to read its task folder and inbox and continue the same work. Busy panes, typed
input and permission prompts are left alone. A pane still showing the provider error gets at most three
resume attempts on separate ticks before normal idle rules apply. An agent whose process
died reopens its own conversation in the same seat; if that cannot resume, the handoff supplies its
successor's context. Recovery preserves the existing task claim. A changed claim owner or execution
generation, expired claim lease or changed seat occupant fences out the old agent, so recovery cannot
resume work already taken by a successor.
