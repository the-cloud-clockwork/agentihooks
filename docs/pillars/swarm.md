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
`terminate-agent` target. herdr refuses an at sign in agent names, so the herdr agent name carries a dash in its
place (`engineer-a1b2c3-0002`). An inbox item left for a retired engineer or ci agent moves to its
seat when its task goes on, else it is withdrawn and its sender told; a master's passes to its successor. `scripts/swarm/naming.py` is the only code that builds or parses an
agent name, and a test walks `scripts/` and `hooks/` to keep it so.

## The master

Every swarm that is not stopped keeps exactly one master, named `master@<code>-<n>`. The tick spawns it when the
swarm starts and spawns a new one when its pane dies; it is never nudged or retired for being idle, and it
does not count against the `eng` and `ci` caps. A stopping swarm keeps its master until the last worker leaves.

Its opening prompt makes it join the ledger as orchestrator, with operator writes reaching it as inbox messages
and the tick waking its idle pane, answer every operator chat
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

## Commands

The swarm id is a lowercase slug of letters, digits and dashes, starting with a letter, at most 48 long.

The Swarm panel's controls (`start`, `pause`, `stop`, `stop --now`, `close`, `reopen`, `set` and `lift`) take the operator or the live master of that same swarm, and refuse every engineer, ci and planner agent and every other swarm's master. Each use by the master posts one chat line on the ledger naming each control with its old and new value.

| Command | Effect |
|---|---|
| `agentihooks swarm list` | One line per swarm: state, caps, agent count, repo. |
| `agentihooks swarm tick` | One reconcile pass over every swarm (the timer runs it). |
| `agentihooks swarm <id> controller` | Show the controller owner, epoch and expiry. |
| `agentihooks swarm <id> controller release` | Release this hive's lease so another controller can take over at a higher epoch. |
| `agentihooks controller run [--once]` | Reconcile every registered swarm each minute; `--once` runs one pass. `AGENTIHOOKS_DEPLOYMENT=local` permits spawning; `compose` and `distributed` reconcile without spawning. |
| `agentihooks swarm templates` | One line per swarm template, built-in or user: per lane its cap, agent, model, effort and default kind, then the compact limit. |
| `agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N] [--max-plan-agents N]` | Register a swarm, paused. Defaults: 2 eng, 1 ci, 1 planner. `--template` takes the caps, compact limit and lane map from a template; a cap flag still wins. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> start` | Run: enable the timer and scale up at once. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> url` | Print the `Ledger page: <link>` line, built from `LEDGER_HOST` and `LEDGER_PORT`; when the ledger server is not answering, the line names `agentihooks ledger serve --ensure`; when the server on that port serves another ledger folder, it prints no link and names both folders. |
| `agentihooks swarm <id> pause` | Stop new spawns; running agents continue. |
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
| `agentihooks swarm <id> status [--json]` | Config, task counts, when the last automatic snapshot was taken (`snapshots  last automatic snapshot 2026-10-05 13:15 UTC  every 30 min  kept 4`), one row per agent, and the health findings. An agent's model and effort are what its running session last reported (the Claude status line, a Codex hook) once that differs from the launch, with model source `session`, so a manual `/model` or `/effort` switch shows on the next tick. |
| `agentihooks swarm <id> autoscale [--fixture <file>] [--json]` | Read only: the autoscale decision (lane ceilings, pending raise, host room and reason) for the live swarm, or for a fixture of accounts and host readings. It never refreshes quota or writes the stored decision; `applied` is false when the swarm runs manual scaling. |
| `agentihooks swarm <id> names [--json]` | The swarm code, its herdr space and every agent name the swarm gave, each with its type, number, session id and spawn and retire times. |
| `agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N max-plan-agents=N` | Change the caps; `swarm <id> max-eng-agents=N` also works. |
| `agentihooks swarm <id> set compact-limit=N` | Launch this swarm's next agents with `AGENTIHOOKS_COMPACT_LIMIT=N` (thousands of tokens); 0 keeps the default. |
| `agentihooks swarm <id> set autonomy=LEVEL` | Set how far agents go without the operator: `manual` engineers open a draft pull request and stop for the operator; `assist` engineers open a pull request and merge only after an operator approval line on the ledger; `delegate` (default) engineers merge on green checks; `full` is delegate, and the master also turns follow ups into tasks without asking. Agents spawned next get it in their prompt and as `AGENTIHOOKS_SWARM_AUTONOMY`; the ledger page swarm panel shows it. |
| `agentihooks swarm <id> set master-agent=claude\|codex` | Master affinity, the master lane's `agent`. The ledger page's Capacity dropdown writes the same field through Apply. When the value differs from the live master's harness, one durable inbox order reaches the master seat: hand off with Handoff v2 and stop. The tick ends the old process before it starts the successor in the same seat on the chosen harness, with the original profile and the frontier model of that harness. An unchanged value orders nothing. A failed successor launch shows on `swarm status` and the page, keeps the handoff and the seat inbox, and retries each tick. Templates, snapshots and restore carry the value. |
| `agentihooks swarm <id> set effort-min=E effort-max=E` | The effort range every lane agent starts inside, default `medium` to `high`. The classifier's pick, a lane or template effort, a resume and the `init-agent` launch of a swarm lane are each clamped into it; Codex levels map by rank, so `xhigh` counts as `max`. Setting a lane effort outside the range is refused with the range named. The swarm panel caps row edits both ends. |
| `agentihooks swarm <id> set <gate>-gate=deny\|log only\|skip` | Operator or master only, for every gate: `identity`, `watch`, `subagents`, `reruns`, `intent`, `build`, `claim-stop`, `push-stop`, `one-push`, `claims`, `trace-plan`, `talk`. The mode lives in the swarm config; the ledger page's Gates controls beside Capacity write the same field, and `swarm status` reports every gate's current mode as `gate_modes`. Inside a swarm the ledger server, the tick, `swarm trace-plan` and the gate entry point read only the config, so an agent's `AGENTIHOOKS_GATE_<NAME>` is ignored there; it picks the mode only outside a swarm or when the swarm config cannot be read. `deny` refuses the call and logs the denial, `log only` lets the call through and logs the would-be denial, `skip` does not run the check. The old names `enforce`, `observe` and `off` remain aliases. Quote a mode containing spaces, for example `"watch-gate=log only"`. |
| `agentihooks swarm <id> set talk-gate=deny\|log only\|skip` | Operator or master only. The ledger server counts each eng and ci agent's talk writes (comments, chat lines, follow-ups) since its last outcome: a pushed commit, an opened pull request, resolved checks, a task state, pull request or proof change. Past 10, `deny` refuses the write and `log only` (the default) logs the would-be deny in the gate log. Talk owed to the operator and a typed `lift the talk gate` pass. The ledger hook's nudge and Stop gate also restart on an outcome and skip tool calls made inside sub-agents. |
| `agentihooks swarm <id> set intent-gate=deny\|log only\|skip` | Operator or master only. `swarm pr` writes the parent intent (project overview, phase, task) into the pull request body and arms the task's intent check. On the next tick the classifier reads that intent with the pull request title, body, changed files, the task proof and the task's plan slice, resolved the way `agentihooks plan read` resolves it (phase range, plan artifact or Swarm v2 package, without the shared sections), and judges whether the phase can use the change as delivered and whether it weakens anything the phase builds; under 0.3, or a weakening at 0.5 or more, the verdict is fail, with reasons. `python -m scripts.gates.intent_calibration` measures the check on its corpus of retained and control inputs. `deny` returns a failed task to its agent (claimed again, a ledger comment and an inbox item); `log only` (the default) logs the fail in the gate log; `skip` skips the check. The `intent` gate (`python -m scripts.gates intent`) refuses `gh pr merge` and `swarm done` while the verdict is fail, or pending for under two minutes; a check still pending after two minutes passes unchecked and is counted. A pass names the plan lines it judged. A task whose plan passed intent at `swarm trace-plan` opens its pull request on that pass, with no pending hold, and the tick still judges the pull request and replaces the plan verdict. Running `swarm pr` again starts a new check. |
| `agentihooks swarm <id> set snapshot-minutes=N` | While the swarm runs, the tick writes an automatic snapshot every N minutes to `~/.agentihooks/swarm/<id>/snapshots/auto-<ms>.json` and removes the oldest past ten. Default 30; a swarm without the setting takes `AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES`; 0 turns automatic snapshots off. `snapshot.json` from `snapshot` and `stop` is separate and never pruned. |
| `agentihooks swarm <id> set eng-agent=codex eng-model=M eng-effort=E eng-kind=K eng-role=TEXT` | Change one lane field (`ci-` likewise); the next spawn in that lane uses it. |
| `agentihooks swarm <id> save-template NAME` | Write this swarm's caps, compact limit and lane map as the user template NAME. |
| `agentihooks swarm <id> send-message TEXT` | Message to every live agent's inbox; never posts to chat. |
| `agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]` | The master or the operator judges a health finding: `false-positive`, `early-real`, `established`, `insufficient-evidence` or `resolved`. The finding hides for `AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES` (60) and comes back once only if its evidence grew. Over monitoring fires past `AGENTIHOOKS_HEALTH_WATCH_MIN` (20) watch calls since the agent's last action and over `AGENTIHOOKS_HEALTH_WATCH_RATIO` (5) per action; the master, whose job is mostly watching, gets `AGENTIHOOKS_HEALTH_MASTER_WATCH_MIN` (60) and `AGENTIHOOKS_HEALTH_MASTER_WATCH_RATIO` (15). Working on drain fires when a working agent that is not idle stays on an account the quota capacity decision marks closed, or one at or under `AGENTIHOOKS_HEALTH_DRAIN_LEFT` (10) percent routing left, the quota balancer's drain line where the tick may still place work, over `AGENTIHOOKS_HEALTH_DRAIN_MINUTES` (10) after its early quota handoff warning. The `watch` gate (`python -m scripts.gates watch`) refuses the watch call that would cross the same limits, so this finding means the gate leaked; a ledger watch re-arm with no live watcher always passes. |
| `agentihooks swarm <id> lift AGENT GATE` | The operator or the master lets one agent past one gate for an hour, the page's lift button in the Agents table runs it. It arms the lift for that agent and writes the same `lift` row to the gate log as typing `lift the <gate> gate` in the agent's pane; the hook gates and the talk gate honour it. Each agent in `status --json` lists `gates`: every gate that denied it in the last hour and whether a lift holds. Other agents are refused. |
| `agentihooks swarm <id> learned` | List every seat's learned notes, one line each: seat, number, maturity, text. |
| `agentihooks swarm <id> promote SEAT NUMBER MATURITY --reason TEXT` | Raise learned note NUMBER on SEAT (`eng-1` or `eng-1@<id>`) to a higher maturity, keeping who promoted it and why. Any agent of the swarm or the operator may promote to `insight`; only the master or the operator to `canon`. |
| `agentihooks swarm <id> retire SEAT NUMBER --reason TEXT` | Retire learned note NUMBER on SEAT so no later occupant's opening prompt carries it; the note keeps its number and records who retired it and why. Only the master or the operator. |
| `agentihooks swarm <id> culture set FILE` | Replace the swarm's culture with the file's text. Every new occupant of every seat, the master included, reads it in its priming chain. |
| `agentihooks swarm <id> culture show` | Print the swarm's culture. |

### Templates

A template is a JSON file: a `name`, a `compact_limit`, optional `links`, an optional `autonomy`
(`manual`, `assist`, `delegate` or `full`; empty means `delegate`) the swarm is created with, and `lanes` with one entry per lane. Worker lanes are `eng`, `ci` and `plan`; `master` configures the master. The `plan` lane defaults to the `planner` profile and has its own cap, names `planner@<code>-<n>` and seats `plan-<k>@<slug>`. A swarm without planner tasks starts no planners. A Claude planner starts in plan mode (`--permission-mode plan`; `agentihooks claude` then passes `--allow-dangerously-skip-permissions`, since `--dangerously-skip-permissions` would override the named mode) and leaves it with no pane prompt: the PermissionRequest hook allows its `ExitPlanMode` with the plan as the updated input and sets the session back to bypass, because Claude Code keeps the proceed prompt for a bare allow and would otherwise fall back to manual mode. The plan is approved by the plan review below. Codex has no launch flag for plan mode, so a Codex planner launches under a `planner` permissions profile instead (`-c permissions.planner=…` with `-c default_permissions="planner"`): it extends `:read-only`, turns network on and grants write only to the ledger folder (`LEDGER_DIR`, default `~/development-ledger`), `~/.agentihooks/swarm` and `~/scratchpad`. The repository stays read only while `agentihooks ledger` and Redis calls work; `ledger task add` needs the ledger folder because it takes a lock file there. The `trace-plan` and `build` gates stay the second line. Per lane:

| Field | Meaning |
|---|---|
| `role` | Replaces the lane's default role text in the agent's opening prompt; empty keeps the default. |
| `cap` | The lane cap the swarm is created with. |
| `agent` | `claude`, `codex` or `auto`; `claude` and `codex` always spawn that harness; `auto` takes the harness of the account the session rotation picks: the eligible account with the fewest live sessions under its five hour band. Codex is one more account in the rotation, judged on its week alone with the top band. |
| `model`, `effort` | Passed to init-agent for the lane's agents; `auto` keeps init-agent's default. The master lane ignores both: a master always launches on the frontier model at high effort for its harness, opus or gpt-6.1-sol. |
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
| `agentihooks swarm <id> merge queue\|dequeue\|state URL` | Queue a pull request into `dev` on the merge queue at its current head, take it out of the queue, or report its state, head and queue entry as JSON. It goes through `gh api graphql`, so any `gh` with `gh api` works; queue and dequeue run only for a swarm worker, never the master, and refuse a pull request into any other base. |
| `agentihooks swarm <id> branch` | Record the current worktree branch on the task once it is on origin, with its repository (`branch_repo`, the origin remote without credentials), so dependent tasks can start from it. |
| `agentihooks swarm <id> done [--pr URL] [proof flags]` | Close the task with the proof its kind needs; the swarm then closes the session. |
| `agentihooks swarm <id> block NOTE` | Comment the blocker, mark the task `blocked`, end the session. |
| `agentihooks swarm <id> trace-plan` | Trace `plan.md` in your task work folder, one piece per line (`- what \| area, area \| why`), to the task, its phase and the project intent through the classifier. A piece under 0.3 is cut and, once the plan passes, filed as a follow-up. A plan with more than half its pieces cut, or sized above one pull request at confidence 0.7, fails; the second failed plan blocks the task when `trace-plan-gate=deny` (default `log only` logs it). A piece appended to a passing plan is traced alone. Before the task's pull request opens, a plan that does not fail is also judged by the intent check on its kept pieces and plan slice; that verdict is printed as `intent` and logged like a pull request verdict. The verdict lands in `plan-verdict.json`; no classifier answer gives `unchecked`, counted in the gate log. The `build` gate (`python -m scripts.gates build`, on Edit, Write, MultiEdit, NotebookEdit, Serena edit tools and `git commit`) refuses an engineer's or CI agent's edit before a passing verdict for the current `plan.md`, and an edit or staged file outside the kept pieces' areas and the task territory; the work folder, `~/scratchpad` and files outside any git work tree are exempt, and an `unchecked` verdict lets edits through, counted. `build-gate` defaults to `log only`. |
| `agentihooks swarm <id> plan approve PHASE [--note TEXT]` / `plan send-back PHASE --note TEXT` | Decide the slice of a phase in review. At `manual` and `assist` autonomy the operator decides with the **Approve plan** and **Send back** buttons on the phase row, and the command refuses an agent naming them (at `assist` the master posts its recommendation as a phase comment). At `delegate` and `full` only the master decides. Approve moves the phase to building and the tick claims its tasks on the same pass. Send back reopens the plan task, records the note for the planner's steering and counts a round; when the planner finishes again the tick reopens the review. The third send back leaves the plan task done, marks the review escalated and raises a priority naming the notes; only the operator decides after that, and an operator send back reopens the plan task whatever the count. |
| `agentihooks swarm <id> wait [MINUTES] [--on checks PR_URL\|reply ITEM\|task ID] [--reason TEXT]` | Declare a wait. While it holds, the tick counts no idle tick for you, so you are neither nudged nor retired. A checked wait (`--on`) lasts until the tick sees its thing resolve (the checks finish or the pull request closes or merges, the inbox item closes, the task is done or blocked), at most MINUTES or 12 hours; the tick then ends it and sends you an inbox item. A bare wait lasts MINUTES, at most 60. The `claim-stop` gate (`python -m scripts.gates claim-stop`, run from a Stop condition) refuses an eng or ci agent's stop while it holds a claimed or pr task with work owed: its pull request merged (run `done`), checks failed (fix or block) or green (merge), or no pull request and no live wait. Pending checks pass the stop and record a checked wait on them, unless a live wait on a reply or a task already holds, which is kept with its reason. The third block in a row with no outcome between blocks the task. The `push-stop` gate, beside it in the engineer, cicd and qa roles, pushes the agent's task branches itself (any worktree `~/dev/worktrees/<repo>/<agent>[-N]` on its own branch, never `dev` or `main`), records branch and head on the task, and refuses the stop with one fixed message, also sent to the agent's inbox, over uncommitted changes, a push origin refused, or pushed work with no pull request and no ledger line since the push. Planners, plan tasks, the master and the operator's sessions pass. The `one-push` gate, in the same roles on Bash calls running `git` or `gh`, keeps a pull request to one push: an eng or ci agent's `gh pr create` or `gh pr ready` is refused until the `standards-reader` and `spec-reader` sub-agents ran for its task (Claude sessions only) and its checkout is committed and on origin, with `--draft` open for the block path; a `git push` to the task branch is refused while the task's open pull request has checks running or waits in the merge queue, unless a check is red; a required check that has not reported yet counts as running. A reader counts once launched; the gate cannot see a review close. |
| `agentihooks swarm <id> progress --doing TEXT --ends-when TEXT` | Say what you are doing and when it ends: a ledger line on your task, a line in its work folder `progress.md` and an outcome on the progress signal. The tick raises a quiet flag (`gates/quiet/<agent>`, a gate log `count` row) on an eng or ci agent holding a claimed or pr task with no progress signal (last outcome, work folder progress line, start) for 30 minutes and no live checked wait. While it stands the `quiet` gate (`python -m scripts.gates quiet`, behind a bash flag test on every tool call) refuses every call except `agentihooks ledger`, `swarm` and `msg` commands; `progress` clears it. The stale claim finding reads the same quiet minutes, and each idle tick is a gate log `count` row (`idle-ticks`) for `doctor rates`. |
| `agentihooks swarm <id> handoff DOC [--recap FILE]` | Finish the session but keep the task: the next tick spawns a successor with the document in its prompt. `--recap` adds the recap (what you did, where you stopped, what you promised) to your seat; older recaps are kept. A hook asks for it when the session reaches `AGENTIHOOKS_COMPACT_LIMIT` thousand tokens (default 600). |
| `agentihooks swarm <id> park DOC` | Park a stacked task whose dependency is still open. Refuses unless the task's `branch` is on origin at the worktree head, the worktree holds no uncommitted changes or untracked files and, where the repo has issues, the task has an issue. Checks the document like `handoff`, writes `parked_on` (the open dependencies) and `stacked_base` (where the branch left the newest dependency branch; for a dependency whose `branch_repo` is another repository, fetched from there, the task's own fork from `origin/dev`), records those other repositories in `parked_repos`, comments the issue with the branch and the ledger task with the blocker, stores the handoff, ends the session and removes the worktree with `wt.sh done --pushed`, which keeps the pushed branch, since the next engineer cuts its own from the pushed branch. The tick reopens the task, which waits until its blockers are done. |
| `agentihooks swarm <id> restack` | After the parked blockers finish, fetch origin and rebase task work from `stacked_base` onto `origin/dev`. Run in a clean branch cut from the parked branch. Success clears `parked_on`; a conflict lists the files and retains the parked list. Resolve the files, run `git rebase --continue`, then run restack again to finish. |
| `agentihooks swarm <id> learned TEXT [--maturity data\|note\|insight\|canon]` | Add a lesson to your seat's learned notes, kept for every later occupant. The maturity defaults to `note`; only the master writes `canon`. |
| `agentihooks swarm <id> say TEXT [--to NAME\|eng\|ci]` | Post to the swarm chat. |

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

## The minute tick

A systemd user timer (`agentihooks-swarm.timer`) runs `agentihooks swarm tick` every minute. `start` installs
and enables it. Every swarm ticks in its own thread; while one swarm's tick runs long, a quicker swarm ticks
again each minute until every swarm's first tick of the pass ends; no extra tick starts after seven minutes.
Each tick, per swarm:

1. Retire agents that finished, and pass on the messages they left (see [Safe retire](#safe-retire)).
2. Free the tasks of agents whose pane is gone, or that stayed idle for 10 ticks. An idle agent is nudged at 3.
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
11. Wake idle panes holding unread items, and climb the wake ladder for items nobody reads.
12. Write the automatic snapshot when it is due.

Each tick renews the controller lease for three ticks. A tick is 60 seconds unless
`AGENTIHOOKS_CONTROLLER_TICK_SECONDS` sets another interval of at least one second. Set it only on the controller
of an `agentihooks controller run` install; `swarm tick` refuses to run with it set, because the host timer ticks
every 60 seconds. Redis server time determines expiry. The owner comes
from `SWARM_HIVE_ID`, falling back to the hostname. Ledger and spawn writes carry the epoch; stale controllers
are refused. The tick lock prevents two ticks in the same epoch and lets a higher epoch take over.

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

### Safe retire

The tick retires an agent for idleness only after 10 idle ticks, and a tick counts as idle only when all three
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
rules: lane, phase, open dependencies, territories and live claims still decide whether a task is claimable, so an
urgent task never skips a dependency that is not done and never takes a task or a territory another agent holds.
Among open tasks whose territories overlap, the higher rank claims first. The rank is read from the ledger on every
pass, so a change applies on the next tick.

Set it with `agentihooks ledger task add ... --rank R` or `task set ID rank=R`, or from the select on each task row
of the ledger page, which lists open tasks in claim order. The master, a planner and the operator may set it; an
engineer or CI agent is refused and proposes the change as a follow up. The queue rank is separate from the
Priorities panel, which holds decisions waiting on the operator.

A task filed under the wrong phase moves with `task set ID phase=P`. The master and a planner may move it; an
engineer or CI agent is refused, and so is a phase the ledger does not hold. The move is stamped and recorded as a
`task moved` event. Each intent verdict records the phase it was judged under, and a verdict from another phase is
judged again on the next tick against the task's current phase.

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

Every message goes through the agent inbox. An addressed line leaves one pending inbox item per recipient,
sent by the real author; an unaddressed agent line only shows on the page. The receiver gets the item at its
next tool call, an idle pane is prompted by the next tick, and unread items climb to the master, then to the
operator. A reply to the operator is posted on the page chat and its item closed. The page keeps the whole
conversation.

Every operator write on the page reaches the inbox the moment the ledger server applies it: a comment or reply,
an answer, a note, a check or uncheck, a chat line. A write on a task goes to the agent that claimed it, a chat
line to its addressee, everything else to the master's seat; an addressee that is gone falls back to the master.
A chat line sent To swarm (`@swarm`) is one item for every live agent, master included, with the same text and
images; the page shows it once, marked as sent to the whole swarm. Only the master's item is work it must answer;
every other agent's copy is information only and asks for no reply. With no live master the item waits at the
master seat for the next one, and the tick tells the page chat that the master is down.
A stopped swarm whose master seat holds a pending item is paused, so the tick starts its master and no engineer.
The ledger hook, the ledger watch and the inbox share one seen mark per agent and write, so each write reaches each
agent once, through whichever path shows it first.
