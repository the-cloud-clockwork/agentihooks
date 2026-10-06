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
| `master` | The one agent the operator talks to. It works no task and never edits code, commits or merges. |

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
place (`engineer-a1b2c3-0002`). An inbox item left for a retired agent passes to the next agent of the same type in
that swarm; a master's waits for its successor. `scripts/swarm/naming.py` is the only code that builds or parses an
agent name, and a test walks `scripts/` and `hooks/` to keep it so.

## The master

Every swarm that is not stopped keeps exactly one master, named `master@<code>-<n>`. The tick spawns it when the
swarm starts and spawns a new one when its pane dies; it is never nudged or retired for being idle, and it
does not count against the `eng` and `ci` caps. A stopping swarm keeps its master until the last worker leaves.

Its opening prompt makes it join the ledger as orchestrator under a watch Monitor, answer every operator chat
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

## Commands

The swarm id is a lowercase slug of letters, digits and dashes, starting with a letter, at most 48 long.

| Command | Effect |
|---|---|
| `agentihooks swarm list` | One line per swarm: state, caps, agent count, repo. |
| `agentihooks swarm tick` | One reconcile pass over every swarm (the timer runs it). |
| `agentihooks swarm templates` | One line per swarm template, built-in or user: per lane its cap, agent, model, effort and default kind, then the compact limit. |
| `agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N] [--max-plan-agents N]` | Register a swarm, paused. Defaults: 2 eng, 1 ci, 1 planner. `--template` takes the caps, compact limit and lane map from a template; a cap flag still wins. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> start` | Run: enable the timer and scale up at once. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> url` | Print the `Ledger page: <link>` line, built from `LEDGER_HOST` and `LEDGER_PORT`; when the ledger server is not answering, the line names `agentihooks ledger serve --ensure`. |
| `agentihooks swarm <id> pause` | Stop new spawns; running agents continue. |
| `agentihooks swarm <id> stop` | Drain: no new spawns, the swarm stops when its agents finish. Both forms of stop take a snapshot first. |
| `agentihooks swarm <id> stop --now` | Kill every agent and reopen its unfinished task. |
| `agentihooks swarm <id> close [--note TEXT] [--now]` | Close the ledger, with open tasks, follow ups or questions left or not: write a Summary section into the ledger overview, built from the ledger (merged tasks with their pull request links, tasks still open, follow ups still open, questions unanswered, phases done out of total, `--note` as a plain words paragraph on top), take a snapshot, retire every agent with the master last, put claimed tasks back to open, mark the ledger closed with `closed_at` and leave the swarm stopped. Settings, culture, seats and learned notes stay. While a master is live, `close` (and the ledger page's Close button, which runs the same command) hands the close to it as an inbox item instead; the master writes its paragraph and runs `close --note`. Without a live master, run by the master itself or with `--now`, it closes at once. The page then shows a Closed banner and HOME lists the ledger under Closed. |
| `agentihooks swarm <id> reopen` | Reopen a closed ledger, retaining its Summary as history and running with the kept settings. A removed swarm is recreated from its newest snapshot. The tick starts a fresh master in the master seat; its prompt carries the summary and asks it to name that summary in its first ledger chat line. Engineers start only for eligible open tasks. The Closed banner and closed HOME card both run this command. A master still retiring must exit first. With no task left to start, the swarm drains and the master stays online. |
| `agentihooks swarm <id> take-master [--replace]` | Run inside any Claude or Codex session to make it the swarm's master; the `take-master` skill runs it when the operator types "you are the master of ledger <id>". Refuses while another master is live; `--replace` retires that master first, and a dead master record is dropped. A session without `AGENTIHOOKS_AGENT_NAME` is named `master@<code>-<n>` on its session registry record, so `terminate-agent`, the tick and the inbox see it under that name. The session occupies the `master@<id>` seat and its agent record keeps the tick from spawning a second master. A closed ledger is reopened and a stopped or stopping swarm set running. Prints the full master priming: seat handoff, culture, recaps, learned notes, ledger summary and standing duties. |
| `agentihooks swarm <id> remove` | Delete a swarm with no agents left: its records and its watch and action counts, so a swarm created again under the same id starts from zero. |
| `agentihooks swarm <id> snapshot` | Write `~/.agentihooks/swarm/<id>/snapshot.json`: the swarm's Redis keys (config and template, agents, claims with their lease, handoffs, name counters), its seats with their history, recaps and learned notes, its culture, the inbox items, pending sets and histories of its seats and agents, a copy of the ledger and each agent's worktree path. |
| `agentihooks swarm <id> restore [--from FILE]` | After a reboot or a lost Redis: refuse while any agent of the swarm is live, write the newest snapshot back (manual, stop or automatic, by the time it was taken; `--from` names an older file) and leave the swarm paused. Each agent with a conversation id, its worktree still on disk (the master: the swarm repo) and its account not out of quota is relaunched through `init-agent --resume` into its own conversation, with the same pane name, seat, task and account (`--route`) on its lane's current model and effort, never the recorded one (a master on the frontier model at high effort); it counts as resumed only once herdr shows that conversation on the new pane, and its first message tells it to re-read its task folder and the ledger before acting. Every failed resume retains its seat and task as awaiting decision. The tick never starts a fresh replacement until the master or operator chooses Fresh on the page or runs `restore-decision`. Resume retries the old conversation; a failed retry keeps the decision open. Each agent's outcome, resumed or awaiting decision with the reason, shows as a `restored` line in `status`, under `restored` in `status --json` and in the Last restore list of the ledger page swarm panel. The ledger copy is written back only when the ledger file is gone. |
| `agentihooks swarm <id> restore-decision AGENT resume\|fresh` | The master or operator chooses how an awaiting seat returns. Resume retries the saved conversation; fresh explicitly permits a new successor. The Last restore list offers the same two controls. |
| `agentihooks swarm <id> confirm-handoff TRANSFER --next TEXT` | A live successor confirms that it read the handoff and its first Next action. Continuity remains pending until that confirmation; binding independently records live seat occupancy. Both appear in Handoff outcomes on the page and in `status --json`. |
| `agentihooks swarm <id> status [--json]` | Config with the Codex share of work-lane spawns against its target (`codex 2/7 spawns 28%  target 30%  min week left 5%`), task counts, when the last automatic snapshot was taken (`snapshots  last automatic snapshot 2026-10-05 13:15 UTC  every 30 min  kept 4`), one row per agent, and the health findings. An agent's model and effort are what its running session last reported (the Claude status line, a Codex hook) once that differs from the launch, with model source `session`, so a manual `/model` or `/effort` switch shows on the next tick. |
| `agentihooks swarm <id> names [--json]` | The swarm code, its herdr space and every agent name the swarm gave, each with its type, number, session id and spawn and retire times. |
| `agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N max-plan-agents=N` | Change the caps; `swarm <id> max-eng-agents=N` also works. |
| `agentihooks swarm <id> set compact-limit=N` | Launch this swarm's next agents with `AGENTIHOOKS_COMPACT_LIMIT=N` (thousands of tokens); 0 keeps the default. |
| `agentihooks swarm <id> set autonomy=LEVEL` | Set how far agents go without the operator: `manual` engineers open a draft pull request and stop for the operator; `assist` engineers open a pull request and merge only after an operator approval line on the ledger; `delegate` (default) engineers merge on green checks; `full` is delegate, and the master also turns follow ups into tasks without asking. Agents spawned next get it in their prompt and as `AGENTIHOOKS_SWARM_AUTONOMY`; the ledger page swarm panel shows it. |
| `agentihooks swarm <id> set codex-share=PCT codex-min-week-left=PCT` | Share of `auto` lane spawns sent to Codex. While Codex spawns are below `codex-share` percent of the swarm's eng and ci spawns, the best signed-in Codex account (the default login is `default`) has at least `codex-min-week-left` percent of its weekly quota left, and Codex has a free session slot, an `auto` lane spawns Codex; otherwise the priority choice applies. Defaults 30 and 5; a swarm without the setting takes `AGENTIHOOKS_SWARM_CODEX_SHARE` and `AGENTIHOOKS_SWARM_CODEX_MIN_WEEK_LEFT`. The master is never part of the share. |
| `agentihooks swarm <id> set talk-gate=enforce\|observe\|off` | Operator only. The ledger server counts each eng and ci agent's talk writes (comments, chat lines, follow-ups) since its last outcome: a pushed commit, an opened pull request, resolved checks, a task state, pull request or proof change. Past 10, `enforce` refuses the write and `observe` (the default) logs the would-be deny in the gate log. Talk owed to the operator and a typed `lift the talk gate` pass. The ledger hook's nudge and Stop gate also restart on an outcome and skip tool calls made inside sub-agents. |
| `agentihooks swarm <id> set intent-gate=enforce\|observe\|off` | Operator only. `swarm pr` writes the parent intent (project overview, phase, task) into the pull request body and arms the task's intent check. On the next tick the classifier reads that intent with the pull request title, body, changed files and the task proof, and judges whether the phase can use the change as delivered; under 0.3 the verdict is fail, with reasons. `enforce` returns a failed task to its agent (claimed again, a ledger comment and an inbox item); `observe` (the default) logs the fail in the gate log; `off` skips the check. The `intent` gate (`python -m scripts.gates intent`) refuses `gh pr merge` and `swarm done` while the verdict is fail, or pending for under two minutes; a check still pending after two minutes passes unchecked and is counted. Running `swarm pr` again starts a new check. |
| `agentihooks swarm <id> set snapshot-minutes=N` | While the swarm runs, the tick writes an automatic snapshot every N minutes to `~/.agentihooks/swarm/<id>/snapshots/auto-<ms>.json` and removes the oldest past ten. Default 30; a swarm without the setting takes `AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES`; 0 turns automatic snapshots off. `snapshot.json` from `snapshot` and `stop` is separate and never pruned. |
| `agentihooks swarm <id> set eng-agent=codex eng-model=M eng-effort=E eng-kind=K eng-role=TEXT` | Change one lane field (`ci-` likewise); the next spawn in that lane uses it. |
| `agentihooks swarm <id> save-template NAME` | Write this swarm's caps, compact limit and lane map as the user template NAME. |
| `agentihooks swarm <id> send-message TEXT` | Operator message to the swarm chat. |
| `agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]` | The master or the operator judges a health finding: `false-positive`, `early-real`, `established`, `insufficient-evidence` or `resolved`. The finding hides for `AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES` (60) and comes back once only if its evidence grew. Over monitoring fires past `AGENTIHOOKS_HEALTH_WATCH_MIN` (20) watch calls since the agent's last action and over `AGENTIHOOKS_HEALTH_WATCH_RATIO` (5) per action; the master, whose job is mostly watching, gets `AGENTIHOOKS_HEALTH_MASTER_WATCH_MIN` (60) and `AGENTIHOOKS_HEALTH_MASTER_WATCH_RATIO` (15). The `watch` gate (`python -m scripts.gates watch`) refuses the watch call that would cross the same limits, so this finding means the gate leaked; a ledger watch re-arm with no live watcher always passes. |
| `agentihooks swarm <id> learned` | List every seat's learned notes, one line each: seat, number, maturity, text. |
| `agentihooks swarm <id> promote SEAT NUMBER MATURITY --reason TEXT` | Raise learned note NUMBER on SEAT (`eng-1` or `eng-1@<id>`) to a higher maturity, keeping who promoted it and why. Any agent of the swarm or the operator may promote to `insight`; only the master or the operator to `canon`. |
| `agentihooks swarm <id> culture set FILE` | Replace the swarm's culture with the file's text. Every new occupant of every seat, the master included, reads it in its priming chain. |
| `agentihooks swarm <id> culture show` | Print the swarm's culture. |

### Templates

A template is a JSON file: a `name`, a `compact_limit`, optional `links`, an optional `autonomy`
(`manual`, `assist`, `delegate` or `full`; empty means `delegate`) the swarm is created with, and `lanes` with one entry per lane. Worker lanes are `eng`, `ci` and `plan`; `master` configures the master. The `plan` lane defaults to the `planner` profile and has its own cap, names `planner@<code>-<n>` and seats `plan-<k>@<slug>`. A swarm without planner tasks starts no planners. Per lane:

| Field | Meaning |
|---|---|
| `role` | Replaces the lane's default role text in the agent's opening prompt; empty keeps the default. |
| `cap` | The lane cap the swarm is created with. |
| `agent` | `claude`, `codex` or `auto`; `claude` and `codex` always spawn that harness; `auto` sends the swarm's Codex share to Codex (`set codex-share`) and otherwise picks the first agent in `AGENTIHOOKS_AGENT_PRIORITY` order with quota and a free session slot. |
| `model`, `effort` | Passed to init-agent for the lane's agents; `auto` keeps init-agent's default. The master lane ignores both: a master always launches on the frontier model at high effort for its harness, opus or gpt-6.1-sol. |
| `kind` | The kind written to a task of this lane that has none when the tick claims it; `auto` writes nothing. |

`links` is a list of `{"from", "to", "kind"}`. `from` and `to` name a seat (`eng-1`) or a lane (`eng`,
`ci`); `kind` is `delegates-to` or `can-observe`. A swarm created from the template keeps them. When it
has any, a sender holding one of its seats may `msg send` or `swarm say` to another of its seats only
along a `delegates-to` link; a `can-observe` link refuses the send and names why. Either kind lets the
sender read that seat's items with `agentihooks msg inbox --of <seat>`. The operator, the master, a
sender holding no seat, and a swarm without links are never restricted.

Built-in templates (`default`, `codex-ci`) ship with agentihooks. User templates live in
`$AGENTIHOOKS_HOME/swarm-templates/` and win over a built-in of the same name. `create --template` stores
the template name and lane map in the swarm config.

A swarm is in one of five states: `running`, `paused`, `stopping`, `stopped`, `drained`. It drains when no
task is left to start and returns to `running` when a new task opens. Lowering a cap never kills work; the
count falls as agents finish.

Agent commands take the agent name from `--as` or `AGENTIHOOKS_AGENT_NAME`:

| Command | Effect |
|---|---|
| `agentihooks swarm <id> issue URL` | Record the task's GitHub issue, where the repo has issues; without them the ledger task is the spec. |
| `agentihooks swarm <id> pr URL` | Record the task's pull request; the task moves to `pr`. |
| `agentihooks swarm <id> done [--pr URL] [proof flags]` | Close the task with the proof its kind needs; the swarm then closes the session. |
| `agentihooks swarm <id> block NOTE` | Comment the blocker, mark the task `blocked`, end the session. |
| `agentihooks swarm <id> trace-plan` | Trace `plan.md` in your task work folder, one piece per line (`- what \| area, area \| why`), to the task, its phase and the project intent through the classifier. A piece under 0.3 is cut and, once the plan passes, filed as a follow-up. A plan with more than half its pieces cut, or sized above one pull request at confidence 0.7, fails; the second failed plan blocks the task when `AGENTIHOOKS_GATE_TRACE_PLAN=enforce` (default `observe` logs it). A piece appended to a passing plan is traced alone. The verdict lands in `plan-verdict.json`; no classifier answer gives `unchecked`, counted in the gate log. The `build` gate (`python -m scripts.gates build`, on Edit, Write, MultiEdit, NotebookEdit, Serena edit tools and `git commit`) refuses an engineer's or CI agent's edit before a passing verdict for the current `plan.md`, and an edit or staged file outside the kept pieces' areas and the task territory; the work folder, `~/scratchpad` and files outside any git work tree are exempt, and an `unchecked` verdict lets edits through, counted. `AGENTIHOOKS_GATE_BUILD` defaults to `observe`. |
| `agentihooks swarm <id> plan approve PHASE [--note TEXT]` / `plan send-back PHASE --note TEXT` | Decide the slice of a phase in review. At `manual` and `assist` autonomy the operator decides with the **Approve plan** and **Send back** buttons on the phase row, and the command refuses an agent naming them (at `assist` the master posts its recommendation as a phase comment). At `delegate` and `full` only the master decides. Approve moves the phase to building and the tick claims its tasks on the same pass. Send back reopens the plan task, records the note for the planner's steering and counts a round; when the planner finishes again the tick reopens the review. The third send back leaves the plan task done, marks the review escalated and raises a priority naming the notes; only the operator decides after that, and an operator send back reopens the plan task whatever the count. |
| `agentihooks swarm <id> wait MINUTES [--reason TEXT]` | Declare a wait (checks, a deploy, a reply) that ends MINUTES from now. While it holds, the tick counts no idle tick for you, so you are neither nudged nor retired. |
| `agentihooks swarm <id> handoff DOC [--recap FILE]` | Finish the session but keep the task: the next tick spawns a successor with the document in its prompt. `--recap` adds the recap (what you did, where you stopped, what you promised) to your seat; older recaps are kept. A hook asks for it when the session reaches `AGENTIHOOKS_COMPACT_LIMIT` thousand tokens (default 600). |
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

A systemd user timer (`agentihooks-swarm.timer`) runs `agentihooks swarm tick` every minute; nothing runs
between ticks. `start` installs and enables it. Each tick, per swarm:

1. Retire agents that finished, and pass on the messages they left (see [Safe retire](#safe-retire)).
2. Free the tasks of agents whose pane is gone, or that stayed idle for 10 ticks. An idle agent is nudged at 3.
3. Reopen claimed tasks that have no agent.
4. Spawn the master if none is online, or retire it once a stopping swarm has no worker left.
5. While `running`, spawn agents up to the caps, one per claimable task, as long as a Claude account has room
   under its session cap.
6. Mark the swarm `stopped` when no agent is left, or `drained` when only the master is and nothing remains to do.
7. Post inbox replies to the operator on the page chat.
8. Run the [ledger event pass](#ledger-event-pass): agent writes and time rules become inbox items.
9. Tick a phase whose tasks are all done, and reopen a ticked phase when a task that is not done lands in it.
   Each change leaves a status comment from `swarm` on the phase and an information item for the master. A phase
   with no task, or out of scope, is left to the master.
10. Send each new health finding to the master with the `verdict` command to judge it.
11. Wake idle panes holding unread items, and climb the wake ladder for items nobody reads.
12. Write the automatic snapshot when it is due.

A lock keeps two ticks from running at once.

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

## Redis

Redis holds the swarm's runtime state, and the swarm refuses to run without it:

- the config of each swarm and the index of swarm ids;
- one claim per task, with a 10 minute lease that each tick renews for live agents, so a task has one owner;
- the agent registry;
- the tick lock.

The default is `redis://127.0.0.1:6379/0`; set `AGENTIHOOKS_SWARM_REDIS_URL` to use another. Task content
(title, lane, state, links) lives in the ledger, not in Redis.

## Talking to the swarm

- Operator: the chat on the ledger page, or `agentihooks swarm <id> send-message "@eng <text>"`. A message with
  no `@` address, or one for an agent not in the swarm, goes to the master's seat. The
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
A stopped swarm whose master seat holds a pending item is paused, so the tick starts its master and no engineer.
The ledger hook, the ledger watch and the inbox share one seen mark per agent and write, so each write reaches each
agent once, through whichever path shows it first.
