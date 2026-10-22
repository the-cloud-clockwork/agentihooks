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

Agents run on [hives](hives.md), the machines joined to the swarm; [Deployment modes](../reference/deployment-modes.md)
says where the controller, ledger and Redis run.

## Agent names

Run `agentihooks swarm rename` to rename every live registered swarm's agents and spaces, or
`agentihooks swarm <id> rename` for one swarm. Running it again makes no changes. Running sessions retain
their launch names; durable aliases keep inbox delivery, crew ownership, terminate selection, waits and
heartbeats working under either name. Herdr uses dashes where canonical names use an at sign, as approved
by the operator. For each swarm with agents, a rename takes the swarm's tick lock for up to ten minutes, so no tick
runs beside it, and fails for a swarm whose tick is running or that another hive controls. A rename killed mid run on
the same machine does not hold up the next tick (see
[Tick lock holder record](#tick-lock-holder-record)).

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

Every swarm that is not stopped keeps at least one master, named `master@<code>-<n>`, and adds more as it grows
(see [Master pool](#master-pool)). The tick spawns it when the
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

### Interactive masters and Remote Control

Claude masters prefer a valid native claude.ai login at launch and handoff;
workers retain their token and API routing. Declare the master account and
optional tier with `agentihooks balance master-account` as described under
[reserved headroom](load-balancing.md#master-accounts-and-reserved-headroom).
The declaration alone neither signs in nor enables Remote Control. A master on
an interactive login has no token to probe; its quota reading comes from its
own sessions, as described under
[Quota readings for an interactive master account](load-balancing.md#quota-readings-for-an-interactive-master-account).

For the lead Claude master to appear in the Claude app, the operator enables
the optional master login and signs in from their own terminal:

```bash
export AGENTIHOOKS_MASTER_CLAUDE_LOGIN=true
agentihooks claude login --master
```

`AGENTIHOOKS_MASTER_CLAUDE_LOGIN` defaults to off and holds for every swarm on
the machine. When on, the front door master of each swarm uses the one private
Claude config home, forces `claudeai` login and receives
`--remote-control "master 1 · <id>"`. Pool masters and workers do not receive this login or the
Remote Control flags. A failed private login, conflicting declared master
account, live login holder or missing worker quota tokens refuses this route;
it does not silently replace the selected private login. See
[Master claude ai login](../reference/account-login.md#master-claude-ai-login)
for isolation, ownership and recovery.

Remote Control connects to the same running local conversation from
[claude.ai/code](https://claude.ai/code) or the Claude mobile app. Sign in on
the device with the same account and organization as the master. The local
Claude process must remain running and have network access. According to the
[Claude Code Remote Control requirements](https://code.claude.com/docs/en/remote-control#requirements):

- A Pro, Max, Team or Enterprise subscription and a native claude.ai login are
  required. Team and Enterprise also need the organization owner to enable
  Remote Control. A routed setup token or API key does not supply this login.
- Bedrock, Google Cloud and Foundry routes, a custom `ANTHROPIC_BASE_URL`, and
  enterprise Claude apps gateways block Remote Control.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` or `DISABLE_GROWTHBOOK` blocks it.
  Organizations requiring Trusted Devices also require device enrollment and
  a recent sign-in. Consult the upstream requirements for version restrictions
  when telemetry is disabled.
- The launch directory must be trusted. AgentiHooks normally establishes
  trust before launch; an untrusted noninteractive launch cannot answer the
  native trust prompt.

Inspect `agentihooks swarm <id> status` and the ledger master card for the
recorded launch decision:

| Status or finding | Meaning |
|---|---|
| `Remote Control: on (master 1 · <id>)` | The launch requested Remote Control with this session name; verify the session in the Claude app before relying on phone access. |
| `Remote Control: off (flag)` | The master login option is off. |
| `Remote Control: off (fallback auth)` | The master uses subscription token authentication. |
| `Remote Control: off (gateway)` | The master uses the API route. |
| `master-fallback-auth/<seat>` | An active master wanted interactive authentication but runs on fallback auth: phone access is lost. Returning to interactive auth removes this finding. |
| `master-rc/<seat>` | A Remote Control failure notification was recorded since this master started: phone access is lost even if the launch decision was on. A `Remote Control back on` notification clears the recorded failure. |

Codex masters have no Claude Remote Control decision. These findings report
lost access; they do not stop the master or change its authentication.

#### Auth fallback and move back

This behavior covers one host: the login probe, the placement and the recorded
route all belong to the machine running the swarm tick. It applies outside the
explicitly enabled private login route.

| Master | Tries the login first | Records the desired interactive route |
|---|---|---|
| Front door Claude master (seat `master@<id>`, or no seat) with no ordered route | Yes | Yes |
| Pool Claude master (`master-2@<id>` and later) with no ordered route | Yes | No |
| Any master whose task orders a route (`route_kind`) | No: the ordered route is used | No |
| Codex master, or the successor of a Codex master | No | No |

A master with no harness set takes the login on Claude. A master whose
predecessor ran on Codex stays on Codex, unless its profile is Claude only.

A login is valid when `claude auth status --text` exits zero and prints
`Login method: Claude account`. The probe runs without the routed credentials:
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_BASE_URL`, every `AH_CC_TOKEN_<slug>` and every `CLAUDE_CODE_USE_*`
variable are removed. It runs when the route is chosen at every master spawn,
including a handoff, a quota handoff and a recycle. A failed, timed out or
expired check at that point sends the master to fallback placement. A master
routed to the login is checked once more just before launch; a failure there
refuses that launch as unavailable, and the
[master outage](#master-outage-and-promotion) rules bring a master back.

On fallback the master is placed with normal subscription token placement and
configured API capacity under the
[live share rules](load-balancing.md#live-share-weight). With the default API
weight of zero, the order is interactive login, token pool, then API overflow;
a nonzero weight can select API capacity earlier. The master may use room
[reserved from workers](load-balancing.md#master-accounts-and-reserved-headroom),
even when the declared master account is the only account with room. It cannot
bypass a spent or otherwise ineligible account; if no permitted account has
room, launch fails.

A quota handoff whose successor finds a valid login skips the quota transfer to
another token account and launches on the login. Without a valid login, the
quota handoff moves to another account as for any agent.

The desired route is stored per agent in the swarm's `desired-routes` record,
outside the agent projection, and in the execution record. A resumed master
keeps it, each new placement sets it again and dropping the agent removes it.
The running master never changes credentials: the next handoff or recycle
checks the login again and returns to it when it is valid, with no operator
action. That successor launch clears `master-fallback-auth/<seat>`.

Restore phone access after a fallback, in a shell with none of the routed
credential variables above set:

```bash
claude auth status --text                 # must print: Login method: Claude account
claude auth login                         # renew the native login if it is expired
agentihooks swarm <id> status             # master-fallback-auth/<seat> stays until the next master launch
```

With `AGENTIHOOKS_MASTER_CLAUDE_LOGIN=true`, renew the private login with
`agentihooks claude login --master` instead; see
[Master claude ai login](../reference/account-login.md#master-claude-ai-login).

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
every other phase keeps its owner. Items already delivered stay with the old owner. A tick item, priority, pull
request notice or task health finding about a phase or a task in it reaches the live master owning that phase; any
other item, and one whose owner seat has no live master, reaches the lead. Every operator write that no task
claimant takes reaches the lead, the front door, whatever phase or hive it is about, and so does page chat naming a
backend master; only a sync order and the `@swarm` information copy reach every live agent. The lead hands work to
backend masters over the masters channel. While the lead seat is empty the lowest numbered live master answers.

### Master pool

Masters scale with the swarm so no single master drowns in inbox items. Claude and Codex masters behave
identically: the same seats, pool, channel and duties, and both read the same master role text and swarm-master
skill. Only a Claude front door takes the claude ai login and Remote Control. A swarm below one step behaves exactly as a single master swarm.

- **Sizing.** With no `master-per` rule, the wanted count is one plus the unfinished engineer, CI and planner agents
  divided by `masters-step` (default 12), masters not counted: 0 to 11 agents one master, 12 two, 24 three. Every
  master has the same authority; the operator and Priorities stay with the front door.
- **Seats.** Seat names come from code. The front door keeps `master@<id>`; backend masters are `master-2@<id>`,
  `master-3@<id>` and on, launched by the same seat launcher as the first master on either harness. Every master's
  Claude session name, herdr tab and Remote Control session read `master <n> · <id>`, with 1 for the front door, so
  masters of one swarm and of different swarms stay apart in Claude.ai and herdr.
- **Cool off.** A rise launches the next backend seat at once. A fall waits until the wanted count stayed lower for
  ten minutes running, and the cool off restarts whenever the count rises. Then every extra backend master is asked
  to hand off at once and the count falls from the top as they leave; an asked seat below a live higher seat retires
  as soon as it is empty. A retired seat's handoff and open inbox items go to the front door. The front door never retires by
  scaling.
- **Inbox pool.** With two or more live masters, items for the master seat from agents and the tick wait in one
  pool per swarm, first in first out, and each is claimed by exactly one live master in round robin seat order,
  skipping a master that is gone or being retired by the tick; a backend master asked to hand off by a shrink keeps taking items until it
  leaves. An unconfirmed item goes to the next master with
  the same id. Items from the operator bypass the pool and go to the front door. Master inbox pool below gives
  every rule.
- **Front door.** Only `master@<id>`, or the lowest numbered live master while that seat is empty, faces the
  operator: it receives page chat and every operator write, holds Priorities and, with
  `AGENTIHOOKS_MASTER_CLAUDE_LOGIN` on, the claude ai login and Remote Control on a Claude front door; Codex masters take neither. A backend master's page chat write is refused
  naming the front door, and its launch carries no login route and no Remote Control. The operator reaches a backend
  master through its herdr pane, or through the front door.
- **Private channel.** Masters sync with `agentihooks swarm <id> masters-channel say TEXT` and
  `agentihooks swarm <id> masters-channel read`; only master seats send and read, and engineer, CI and planner
  sessions are refused. It carries sync only: an item that arrived without context, or a fact another master holds.
  The front door answers the operator about the backend masters from it.
- **Intent grouping.** `MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS` (on by default; `false` turns it off) runs the classifier. Each
  master keeps its last 15 delivered items; every waiting pool item is compared for intent against each master's
  items in parallel, using only first lines, the first configured API classifier model falling through to the next on a backend failure, one short verdict per master and a cache
  by item id. A match sends the item to that master, so related items reach the same master together. Any
  classifier error, timeout (`MASTER_INBOX_CLASSIFIER_TIMEOUT_S`, default 3 seconds) or low verdict falls back to
  round robin; a classifier fault never holds an item. With the flag off, delivery is plain round robin.
  [Intent grouping](#intent-grouping) gives every rule.

#### Sizing settings

`master-per`, `masters-step` and the manual `masters=N` count decide the master count. The tick reads them in this
order:

| Setting | Effect |
|---|---|
| `master-per=N\|hive` | Overrides the step: one seat per N working agents rounded up, or one per hive. |
| `masters-step=N` (N of 1 or more) | One master plus the working agents divided by N, rounded down. |
| `masters-step=0` | With `master-per=off`, no scaling: the count stays the manual `masters=N` and the tick only fills empty seats. |
| `master-per=off` | The default. Hands sizing back to `masters-step`. |

`swarm create` stores `masters-step=12`. A swarm created before the setting existed loads `0`, so it keeps its manual
count until the step is set. A stopping or stopped swarm never scales; the tick only fills its seats.

```bash
# Read the step; the first status line includes "masters step 12"
agentihooks swarm my-swarm status

# One extra master per 8 working agents instead of per 12
agentihooks swarm my-swarm set masters-step=8

# Pin three masters; this also sets the step to 0
agentihooks swarm my-swarm set masters=3

# Turn scaling back on, after a pinned count or for a swarm created before the setting
agentihooks swarm my-swarm set masters-step=12
```

A `masters=N` and a `masters-step=N` in the same command keep the step given, and the step then sizes the count.

`set` prints the stored settings as JSON, `masters_step` among them, and runs one tick at once on a running swarm,
so a rise launches its seat in the same command. On the ledger page the Capacity row shows **masters step** after
the engineer, CI and planner caps, with `−` and `+` buttons and a number field. Apply writes it through the same
setting and accepts 0 to 50.

Working agents are the swarm's agents outside the master lane that have not finished: engineer, CI and planner
agents, and any other lane a template adds. With the default step:

| Working agents | Wanted masters | Seats |
|---|---|---|
| 0 to 11 | 1 | `master@<id>` |
| 12 to 23 | 2 | adds `master-2@<id>` |
| 24 to 35 | 3 | adds `master-3@<id>` |

#### Scaling over time

With the default step, a swarm grows from 11 to 12 working agents. The next tick sets the seat count to two,
launches `master-2@<id>` and prints `master seats 1 to 2 for 12 working agents`. On the masters channel, every master
reads `Master seats are now 2 for 12 working agents. Phase owners: ...`, naming the phases each seat owns.

The swarm then falls back to 11 agents. The count stays at two while the cool off runs, and a return to 12 agents
ends the cool off with no change. A rise that stays below the seat count restarts it instead: a swarm holding three
seats that falls to 11 agents (wanted one) and climbs back to 12 (wanted two) starts its ten minutes again. After ten
minutes at a lower wanted count:

1. The tick sends each extra backend master an inbox item from `swarm`: write a Handoff v2 for the lead master, run
   `agentihooks swarm <id> handoff <doc>` and stop. The tick prints
   `asked <agent> to hand off retiring seat master-2@<id>`.
2. When that master has left its seat, the tick retires the seat. Its handoff document reaches `master@<id>` as an
   inbox item. Every open item still at the seat is redirected to `master@<id>`, except the retire request, which
   is withdrawn. The tick prints `retired master seat master-2@<id> after <seconds> seconds`.
3. The seat count falls, phase owners rebalance and the masters channel announces the new owners.

An extra backend seat that is empty and has no live seat above it retires at once when the cool off ends, and the
tick prints
`closed empty master seat <seat>`. If the swarm grows again before a retiring master leaves, its retire request is
withdrawn with the note `the swarm grew again, so seat <seat> stays`.

The swarm's master seat record keeps the seat count, and a `retire-seconds` list beside it keeps each retirement
time, in seconds from the retire request to the retirement. Both are written to the `master_scaling` metrics table as
`master_seat_count` and `master_retire_seconds`.

#### Master scaling troubleshooting

| Symptom | Cause and fix |
|---|---|
| A swarm with 30 agents keeps one master. | Its step is `0`: it was created before the setting, or a later `set masters=N` reset it. Run `set masters-step=12`. |
| The count did not fall when agents finished. | The cool off has not run ten minutes at the lower count, or a rise restarted it. A count falls only past seats whose master has left; a master asked to hand off keeps its seat until it runs `swarm <id> handoff`. |
| `master-2@<id>` still answers after the step changed. | The cool off has not run ten minutes, the asked master has not handed off yet, the swarm is stopping or stopped and so never shrinks, or a `master-per` rule overrides the step (run `set master-per=off`). |
| The front door seat never retires. | That is by design: scaling only retires backend seats, highest number first. |

### Master inbox pool

The lead address `master@<id>` is the swarm's front door. While two or more masters are live, an item sent to
`master@<id>` by any sender other than `operator` (a tick item, an agent's question or follow up, a
`msg send master@<id>`) does not wait at the lead's seat. It enters one pool per swarm and is handed to exactly one
live master:

| Rule | Behavior |
|---|---|
| Order | Items leave the pool oldest first, by the arrival sequence number the inbox gives each item sent to `master@<id>`. |
| Exactly once | A master takes an item in one Redis transaction that removes it from the pool, moves it pending to that master's seat and records the seat as its holder. Two processes handing out the same pool never give one item to two masters. |
| Round robin | The pool remembers the last seat it served and gives the next item to the next live seat in seat order, lead first, then `master-2@<id>`, `master-3@<id>` and on, wrapping back to the first. |
| Skips | A seat whose master is finished, retiring, under a tick retire request (a shrink handoff ask is not one), retired early, numbered above the seat count with no live master record, or recorded as having left its seat is not live and is passed over. |
| Redelivery | A pooled item its holder does not confirm within `AGENTIHOOKS_INBOX_REDELIVER_S` (default 300 seconds) goes back to the pool and on to the next live master, passing over the master that let it lapse while another is live. It keeps its id. |
| Gone holder | Items a master claimed that are still pending at its seat return to the pool when that master stops being live, for the remaining masters. Items it had already been delivered return at the next redelivery pass, as any unconfirmed item does. |
| No master | Items already in the pool wait there, in order, until a master is live. A new item sent while fewer than two masters are live does not enter the pool and waits at the lead seat. |

The inbox hands out the pool when an item enters it and again on every redelivery pass, so an item waiting for a
master, or one whose holder lapsed, moves within a redelivery pass. Each move is a line on the item's history:
`claimed from the master pool by <seat>` when a master takes it, and `returned to the master pool: its master is
gone` when it comes back from a master that left. While an item waits in the pool it belongs to no master. A
master other than the front door seat is refused with `message <id> belongs to master@<id>, not <name>`; for the front
door seat, `msg read` and every state change other than a close are refused with `message <id> waits in the master
pool for the next master`. Closing it (`msg close <id> cancel`) takes it out of the pool. An item redirected to another address
leaves the pool and stays where it was sent.

Operator facing items never enter the pool. Page chat, every operator write on the ledger page (comments, replies,
answers, notes, checks) and every relayed decision are sent as `operator` and reach the front door master: the lead,
or the lowest numbered live master while the lead seat is empty. An item addressed to a numbered seat, such as
`master-2@<id>` for a phase that seat owns, reaches that seat directly.

A swarm with one live master has no pool: items for `master@<id>` wait at its seat and redeliver to it exactly as in
a single master swarm. When a second master goes, the remaining master takes the items still pooled at the next
redelivery pass, and a lapsed item of its own redelivers to it as before.

The pool, the master write fence and the operator mail routing read the live master seats from one reader, built on
the seat registry: a seat counts while its registered occupant has neither left it, finished nor retired, and a master
record whose seat the registry does not hold yet counts by its record. During a handoff the successor that took the
seat counts and its predecessor does not, so all three agree on the same seat list and the same lead. The page chat
front door gate reads the seat registry too: a master writes page chat only while its seat pointer in the registry
names the lead seat or names no seat, as for a master that predates seats. A registry read that fails lets the write
through and records it in the gate log.

Inspect a pool with the inbox commands and, for the raw state, Redis (the key prefix is
`AGENTIHOOKS_SWARM_KEY_PREFIX`, default `agentihooks`):

```bash
agentihooks msg inbox --of master@<id>
agentihooks msg inbox --of master-2@<id>
redis-cli ZRANGE agentihooks:inbox:pool:<id> 0 -1 WITHSCORES
redis-cli GET agentihooks:inbox:pool:<id>:last
redis-cli SMEMBERS agentihooks:inbox:pool:<id>:holders
```

The sorted set lists the item ids still waiting, scored by arrival sequence number; `last` is the seat served last
and `holders` the seats that have claimed pool items, kept until a seat stops being live and its pending items
return. An item that stays in the pool while masters are live, or a master that never receives
pool items, points to the live seat list: check `swarm <id> status` for finished, retiring or retired masters, and the item's
history, which `msg read <id>` prints for the master that claimed it, for the `claimed from the master pool by <seat>`
line naming that seat. `msg read` also marks the item read, so run it only as the master taking up the item.

#### Intent grouping

Intent grouping sends a pooled item to the master that recently handled related work, so a run of items about one
task, pull request or question reaches one master instead of being dealt across all of them. Round robin stays the
rule for every item the classifier cannot place.

| Setting | Default | Meaning |
|---|---|---|
| `MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS` | on (unset) | `1`, `true`, `yes` or `on`, in any case and with surrounding spaces, keeps grouping on; any other value, such as `false`, `0` or an empty string, turns it off |
| `MASTER_INBOX_CLASSIFIER_TIMEOUT_S` | `3` | Seconds one pool pass waits for all of its verdicts. An unreadable value keeps 3 |

Both are read from the environment of the process that hands out the pool: the sender of an item to `master@<id>`
(an agent's `msg send`, a question or follow up, a tick item) and the inbox redelivery pass. Set them where those
processes get their environment, such as the profile `env` block or an `.env` file under the AgentiHooks home, so
every sender agrees. The verdicts come from the [decision classifier](../reference/classifier.md), which needs
`AGENTIHOOKS_CLASSIFIER_URL` and `AGENTIHOOKS_CLASSIFIER_LITELLM_KEY`.

```bash
# turn grouping off for this shell and everything it starts
export MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS=false
# give the classifier five seconds per pool pass
export MASTER_INBOX_CLASSIFIER_TIMEOUT_S=5
```

How an item is placed, on every pool pass (each send into the pool and each redelivery pass):

1. **Recent items.** Each live master keeps the first lines of the last 15 items the pool handed to it, newest first.
   Every claim adds one, whether intent or round robin chose the master. The list expires 24 hours after its last
   addition. A master with no recent items is not asked about, so the first items of a new swarm go round robin.
2. **Every waiting item is compared.** The pass takes every item still waiting in the pool, the new one and older
   ones alike, and asks one yes or no question per item and master: does the new item continue the same work, task
   or question as one of that master's recent items. All questions of one pass run in parallel, at most 16 at a time,
   under one deadline of `MASTER_INBOX_CLASSIFIER_TIMEOUT_S`.
3. **Match.** A verdict of 0.7 or more is a match. The item goes to the matching master with the highest verdict;
   equal verdicts go to the seat whose name sorts last. Without a match the item takes its round robin turn.
4. **Live seats only.** A matched master that is no longer live, or the master that just let the item lapse on
   redelivery, is passed over and the item takes its round robin turn. Operator items never enter the pool and are
   never classified.

What one item costs:

| Saving | How |
|---|---|
| First lines only | The question carries the item's first line, cut to 120 characters, and the master's recent first lines. The rest of the item never leaves the inbox. |
| One verdict per master | Each question asks for one probability, never a choice list or a score. |
| API models only | The question goes to the classifier API models in `AGENTIHOOKS_CLASSIFIER_MODELS` order. A failed model passes the question to the next one, except a 400 that does not name context or tokens, which ends that question with a request error. When every model fails or the key is refused, the API is marked down for `AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S` (default 120 seconds) and every verdict goes round robin until it clears. The [classifier failover table](../reference/classifier.md#failover) has every case. The Haiku and Luna command line fallbacks are not used, so no agent session is started for a verdict. |
| Cache by item | Verdicts are stored in one hash per item, one field per master and exact recent list. A later pass asks again only when that master's recent items changed. The hash expires one hour after the last pass that asked a question for the item. A verdict that lands after the deadline is still stored for the next pass. |

The token estimate is the JSON request length divided by four. With 15 recent lines of 120 plain ASCII characters,
one question is estimated at no more than 700 tokens, so such an item costs about 700 tokens per live master that has
recent items, once per change of that master's recent list. Quotes and non-ASCII characters are escaped in JSON and
raise the estimate, up to several times for text that is mostly non-ASCII.

Any classifier error, any question unanswered at the deadline, and any verdict under 0.7 leaves the item to round
robin in the same pass. A slow classifier delays the pass by at most the timeout and never keeps an item in the pool.
With the flag off no question is asked and no recent items are recorded; delivery is plain round robin.

A new item enters the pool only while two or more masters are live, but a redelivery pass classifies the items
already waiting there whatever the live count, provided a live master has recent items. With one live master those
questions change nothing, since that master takes every item, yet they still appear in the decision log and the
`tokens` counters.

Inspect grouping in Redis (prefix `AGENTIHOOKS_SWARM_KEY_PREFIX`, default `agentihooks`) and in the classifier decision
log, where every question is recorded under purpose `master-inbox-intent`:

```bash
redis-cli LRANGE agentihooks:inbox:pool:<id>:recent:master-2@<id> 0 -1
redis-cli HGETALL agentihooks:inbox:pool:<id>:intent:<item id>
agentihooks classifier stats --purpose master-inbox-intent
```

The `recent` list holds the first lines a master was handed. The `intent` hash holds one field per master and recent
list, `<seat>|<hash of the recent list>`, whose value is that verdict, and a `tokens` field counting the estimated
tokens sent for the item over every pass.

| Symptom | Check |
|---|---|
| Related items still spread across masters | `classifier stats` shows calls and their sources. Calls with no source mean no model answered: check `AGENTIHOOKS_CLASSIFIER_URL`, the key and the down marker described in the classifier reference. No calls at all means grouping is off in the sending process, the pool is empty, no live master has recent items yet, or every verdict was already cached. |
| Items placed correctly but slowly | Every send into the pool waits up to `MASTER_INBOX_CLASSIFIER_TIMEOUT_S`. Lower it; late verdicts still fill the cache for the next pass. |
| A master receives unrelated items | Verdicts below 0.7 cannot route; read the item's `intent` hash for the verdicts each master got. A matched master never takes an item it just let lapse. |

## Commands

The swarm id is a lowercase slug of letters, digits and dashes, starting with a letter, at most 48 long.

The Swarm panel's controls (`start`, `pause`, `stop`, `stop --now`, `close`, `reopen`, `set` and `lift`) take the operator or the live master of that same swarm, and refuse every engineer, ci and planner agent and every other swarm's master. Each use by the master posts one chat line on the ledger naming each control with its old and new value.

| Command | Effect |
|---|---|
| `agentihooks swarm init NAME [--plan FILE] [--content FILE] [--repo DIR] [--template NAME] [--operator-asked WORDS] [--timeout SECONDS]` | The one way to create a swarm. NAME is one or two lowercase words joined by a dash and ending in `swarm`, such as `rig-grade-swarm`; it names both the swarm and its ledger. A missing, invalid or taken name is refused with the rule and two suggestions built from the plan title, before anything starts. It then brings up the local swarm stack, Redis with its password, the ledger server and the controller, from the Compose files packaged with agentihooks into `$AGENTIHOOKS_HOME/swarm-stack`. The first run writes `.env` there with a random Redis password, free ledger and Redis ports and `SWARM_IMAGE_TAG=release`, so the stack runs the released swarm image; later runs keep them and the data. `SWARM_COMPOSE_PROJECT` in that `.env` names the Compose project (default `agentihooks-swarm`), so a second stack home, such as a scratch proof, never touches the first. It waits until every service is healthy (default 300 seconds) and the ledger home page answers. A missing Docker, daemon or Compose plugin, an unhealthy service or a silent ledger page is named with its fix and exits 1. Then it creates the ledger and the swarm inside the stack, with the stack's own code, ledger folder and Redis, so the stack's ledger page serves it: the ledger comes from `--content`, or one automatic phase titled after `--plan` when there is no content, and `sources`, being host paths, stay out of it. In the shared ledger folder only a master seat or the operator may run it, and content with fewer than three phases or tasks needs `--operator-asked` quoting the operator. It never starts the swarm. Running it again for the same swarm prints its ledger page and changes nothing; a name held by a swarm on another repository, or by a ledger that is not a swarm's, is refused with the rule and suggestions. Output is coloured on a terminal, plain when piped or with `NO_COLOR`. |
| `agentihooks swarm names check NAME [--plan FILE]` | Prints `valid: NAME`, or `invalid:` with the reason, the name rule and two free suggestions from the plan title. A name the stack already uses for a swarm, or for a ledger that is not a swarm's, is taken; a swarm ledger left without its swarm stays free so init can finish it. With the stack down only the rule is checked, and the output says so. Exits 1 when invalid. |
| `agentihooks swarm init --prepare-clients` | Stage named authentication before moving an existing local swarm into the bundle. First deploy the bundle's named Redis user and persistent ACL configuration through the source service's repository, retaining existing clients, and verify authentication after a source restart. Preparation checks the bundle credential against the current localhost Redis, its named user and configured ACL file, then atomically writes the private `swarm-client.env` companion for existing environment loaders. It changes no Redis users, data, services, deployment mode, ledger address or ports. Existing sessions keep their connections; new processes use named authentication on the same source. Keep that source as the sole writer until the master completes seat turnover and verifies client counts before the final data and port cutover. |
| `agentihooks swarm init --adopt [--no-start]` | Hand the existing shared ledger folder, user and home to the bundle. Requires an earlier `--prepare-clients` run and refuses without its `swarm-client.env`. It records `SWARM_ADOPT_LEDGER_DIR`, `SWARM_ADOPT_HOME`, `SWARM_ADOPT_UID`, `SWARM_ADOPT_GID`, `SWARM_ADOPT_CODE` (this agentihooks checkout), `SWARM_ADOPT_REDIS_URL` (the Redis the prepared clients use) and `SWARM_PORT=8765` in the stack `.env`; later runs without `--adopt` keep them, and each `--adopt` run writes all seven again. While recorded, every stack Compose call adds `deploy/compose/adopt.yaml`, so the `swarm` service runs as that user on the host PID and network namespaces, bound to 127.0.0.1:8765, from that checkout with reload on, against that Redis, and serves the folder at its own path, so every swarm keeps its ledger, inbox items and seats; `ledger serve --ensure` and `--stop` for that folder, and the Doctor and watchdog restarts, go to the bundle `swarm` service and never restore the legacy `ledger` service of the `agentihooks-ledger` Compose project; another shared folder is refused by name; and the stack Redis `swarm` user is denied `FLUSHALL` and `FLUSHDB`. It moves no data or seats and activates no hive. Cannot be combined with `--prepare-clients`. Rollback: stop the bundle `swarm` service, move the adoption lines out of the stack `.env`, and run `ledger serve --ensure` to start the legacy service again. Details: [Docker Compose quick start](../getting-started/compose-quick-start.md#adopt-an-existing-workstation-ledger). |
| `agentihooks swarm init --no-start` | Check Docker and the Compose plugin, write the stack files, `.env` (adoption included with `--adopt`) and `client.env`, then stop: no service, swarm or ledger is started or created. |
| `agentihooks swarm list` | One line per swarm: state, caps, agent count, repo. |
| `agentihooks swarm tick-watch` | The stale tick watcher, run by its own user service: one inbox alert per stale tick episode to the front door master seat and every live agent seat, a resolved notice when a fresh pass lands, on a workstation a scheduler tick restart and a respawn of an empty front door master while the tick is stale, and in a cluster a placement of an empty front door master (see [Tick heartbeat and stale watcher](#tick-heartbeat-and-stale-watcher)). |
| `agentihooks swarm tick` | One reconcile pass over every swarm (the scheduler runs it every minute). |
| `agentihooks swarm <id> controller` | Show the controller owner, epoch and expiry. |
| `agentihooks swarm <id> controller release` | Release this hive's lease so another controller can take over at a higher epoch. |
| `agentihooks controller run [--once]` | Reconcile every registered swarm each minute; `--once` runs one pass. `AGENTIHOOKS_DEPLOYMENT=local` permits spawning; `compose` reconciles without spawning; `distributed` spawns only engineer and CI work its Kubernetes runtime places, and leaves master, planner, dispatcher and frontend work to the workstation hive. |
| `agentihooks swarm templates` | One line per swarm template, built-in or user: per lane its cap, agent, model, effort and default kind, then the compact limit. |
| `agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N] [--max-plan-agents N]` | The step `swarm init` runs to register a swarm, paused. Typed, it is refused and names `swarm init`, except for proof and demo swarms named by code. Defaults: 2 eng, 1 ci, 1 planner. `--template` takes the caps, compact limit and lane map from a template; a cap flag still wins. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> start` | Run: enable the timer and scale up at once. Ends with the `Ledger page: <link>` line. |
| `agentihooks swarm <id> url` | Print the `Ledger page: <link>` line, built from `LEDGER_HOST` and `LEDGER_PORT`; when the ledger server is not answering, the line names `agentihooks ledger serve --ensure`; when the server on that port serves another ledger folder, it prints no link and names both folders. |
| `agentihooks swarm <id> pause` | Stop new spawns; running agents continue. |
| `agentihooks swarm <id> proof-control --request FILE` | The creating engineer pauses, drains, sets or starts its scratch proof fixture using a JSON request. `{"control":"pause"}` pauses new spawns and `{"control":"start"}` starts the swarm. `{"control":"set","fields":{...}}` queues `swarm set` for allowlisted keys only: `codex-inbox-transport`, `max-eng-agents`, `max-ci-agents`, `max-plan-agents` and each lane's `agent`, `model`, `effort` and `profile`; each value starts with a letter or digit and holds at most 64 letters, digits, dots, colons, underscores or dashes; caps take a whole number below 1000 and the transport `legacy` or `app_server`, and each lane agent `auto`, `claude` or `codex`. Drain carries `control: "drain"`, the exact `agent`, `op`, `incarnation`, `generation` and `task_revision` from the lifecycle view. The command authenticates the engineer on its parent swarm, obtains the fixture credential internally and prints only the control result. The credential permits no other action or swarm, and set and start need a scratch swarm; drain preserves caps and fences replacement workers. |
| `agentihooks swarm <id> stop` | Drain: no new spawns, the swarm stops when its agents finish. Both forms of stop take a snapshot first. |
| `agentihooks swarm <id> stop --now` | Kill every agent and reopen its unfinished task. |
| `agentihooks swarm <id> close [--note TEXT] [--now]` | Close the ledger, with open tasks, follow ups or questions left or not: write a Summary section into the ledger overview, built from the ledger (merged tasks with their pull request links, tasks still open, follow ups still open, questions unanswered, phases done out of total, `--note` as a plain words paragraph on top), take a snapshot, retire every agent with the master last, put claimed tasks back to open, mark the ledger closed with `closed_at` and leave the swarm stopped. Settings, culture, seats and learned notes stay. While a master is live, `close` (and the ledger page's Close button, which runs the same command) hands the close to it as an inbox item instead; the master writes its paragraph and runs `close --note`. Without a live master, run by the master itself or with `--now`, it closes at once. The page then shows a Closed banner and HOME lists the ledger under Closed. |
| `agentihooks swarm <id> reopen` | Reopen a closed ledger, retaining its Summary as history and running with the kept settings. A removed swarm is recreated from its newest snapshot. The tick starts a fresh master in the master seat; its prompt carries the summary and asks it to name that summary in its first ledger chat line. Engineers start only for eligible open tasks. The Closed banner and closed HOME card both run this command. A master still retiring must exit first. With no task left to start, the swarm drains and the master stays online. |
| `agentihooks swarm <id> take-master [--replace]` | Run inside any Claude or Codex session to make it the swarm's master; the `take-master` skill runs it when the operator types "you are the master of ledger <id>". Refuses while another master is live; `--replace` retires that master first, and a dead master record is dropped. A session without `AGENTIHOOKS_AGENT_NAME` is named `master@<code>-<n>` on its session registry record, so `terminate-agent`, the tick and the inbox see it under that name. The session occupies the `master@<id>` seat and its agent record keeps the tick from spawning a second master. A closed ledger is reopened and a stopped or stopping swarm set running. Prints the full master priming: seat handoff, culture, recaps, learned notes, ledger summary and standing duties. |
| `agentihooks swarm <id> master up [--last\|--new\|--replace]` | Run from a plain terminal to bring a master online. Without a flag it shows the last master (name, harness, when it last ran) and asks one question: 1 brings back the last master by reopening its own Claude or Codex conversation (`init-agent --resume`) in the `master@<id>` seat; 2 starts a new master through the tick's launch path, primed with the seat handoff, culture, recaps and learned notes. The last master is the newest master, from the agent registry or the swarm history. When it cannot be resumed (still running, no conversation id, harness not recorded, swarm repo gone, account out of quota, resume did not start) it says why and offers the new master; it never switches silently. An unclear answer is asked again, three tries in all. `--last` and `--new` answer without a prompt; `--last` that cannot resume exits non zero. A saved launch assignment or handoff launch missing a profile, harness, model or effort takes it from the swarm config: the master lane profile (default `master`), the master affinity harness (default claude) and the frontier model at an effort inside the swarm range. A live front door master does not stop it: the new master takes the lowest free master seat (`master-<n>@<id>`) and its own title `master <n> · <id>`, so two masters run side by side on distinct seats. A master seated this way holds no phase until the master seat count reaches its seat. `--replace` (exclusive with `--last` and `--new`) retires the live front door master first and the new one takes `master@<id>`; a front door that will not retire starts nothing. `--last` never resumes onto a live front door: it says why and offers the new master. The master joins the ledger as orchestrator; a closed ledger is reopened and a stopped swarm set paused. Prints the master, pane, seat and choice as JSON; a failed launch prints the reason and exits non zero. |
| `agentihooks swarm <id> <profile> up` | Run from a plain terminal to open any role or overlay profile for yourself: a base role (`planner`, `engineer`, `qa`, `cicd`) or an installed overlay whose chain resolves to one, such as `frontend`. `master` is refused (use `master up`), as is a profile that is not installed or has no base role. It opens a quota routed Claude session in a pane of the swarm's herdr space with that profile, the inbox channel loaded, the frontier model, and `AGENTIHOOKS_SWARM` bound with lane `operator` and no task. The name comes from the swarm name registry by the role's kind (`planner@<code>-<n>`, `engineer@…`, `ci@…`) and its registry entry is marked as an operator launch. It is never put in the agent registry: it claims no task, takes no lane slot, and the tick neither nudges nor retires it; the context recycle gate skips it. It answers to you in its pane. A planner plans with you and, on your accept, appends the plan as manual phases (`ledger plan phases`), publishes it (`ledger publish-plan`), adds tasks to those phases, tells the master by inbox and ends with `swarm <id> exit`. The ledger records who appended each phase (`added_by`), and a planner holding no plan task may add tasks only to phases it appended. Other roles work with you and exit when you say so. Prints the name, pane, profile and role as JSON; a failed launch retires the name, prints the reason and exits non zero. |
| `agentihooks swarm <id> agent-up <profile>` | The same launch as `<id> <profile> up`, in its parser form. |
| `agentihooks swarm <id> exit` | An agent launched with `<profile> up` retires its own name and ends its session. Any other name is refused. |
| `agentihooks swarm <id> remove` | Delete a swarm with no agents left: its records and its watch and action counts, so a swarm created again under the same id starts from zero. |
| `agentihooks swarm <id> snapshot` | Write `~/.agentihooks/swarm/<id>/snapshot.json`: the swarm's Redis keys (config and template, agents, claims with their lease, handoffs, name counters), its seats with their history, recaps and learned notes, its culture, the inbox items, pending sets and histories of its seats and agents, a copy of the ledger and each agent's worktree path. |
| `agentihooks swarm <id> restore [--from FILE]` | After a reboot or a lost Redis: refuse while any agent of the swarm is live, write the newest snapshot back (manual, stop or automatic, by the time it was taken; `--from` names an older file) and leave the swarm paused. Each agent with a conversation id, its worktree still on disk (the master: the swarm repo) and its account not out of quota is relaunched through `init-agent --resume` into its own conversation, with the same pane name, seat, task and account (`--route`) on its lane's current model and effort, never the recorded one (a master on the frontier model at high effort); it counts as resumed only once herdr shows that conversation on the new pane, and its first message tells it to re-read its task folder and the ledger before acting. Every failed resume retains its seat and task as awaiting decision. The tick never starts a fresh replacement until the master or operator chooses Fresh on the page or runs `restore-decision`. Resume retries the old conversation; a failed retry keeps the decision open. Each agent's outcome, resumed or awaiting decision with the reason, shows as a `restored` line in `status`, under `restored` in `status --json` and in the Last restore list of the ledger page swarm panel. The ledger copy is written back only when the ledger file is gone. |
| `agentihooks swarm <id> restore-decision AGENT resume\|fresh` | The master or operator chooses how an awaiting seat returns. Resume retries the saved conversation; fresh explicitly permits a new successor. The Last restore list offers the same two controls. For a Kubernetes worker attempt the controller applies the choice on its next recovery pass; see [Fenced restore decisions](../swarm-v2/checkpoint-restore.md). |
| `agentihooks swarm <id> confirm-handoff TRANSFER --next TEXT` | A live successor confirms that it read the handoff and its first Next action. Continuity remains pending until that confirmation; binding independently records live seat occupancy. Both appear in Handoff outcomes on the page and in `status --json`. |
| `agentihooks swarm <id> status [--json]` | Config, task counts, when the last automatic snapshot was taken (`snapshots  last automatic snapshot 2026-10-05 13:15 UTC  every 30 min  kept 4`), one row per agent with its [transport capabilities](#transport-capabilities), and the health findings. An agent's model and effort are what its running session last reported (the Claude status line, a Codex hook) once that differs from the launch, with model source `session`, so a manual `/model` or `/effort` switch shows on the next tick. It also prints the host room (`host room 3: <reason>`, or `host room unknown` with its reason when capacity is unavailable; see [Deployed host capacity](#deployed-host-capacity)) and, when the last spawn pass stopped at that room, the hold (`holding spawns: host memory room 3, 3 spawned since it was granted: <reason>`). The host room gates every spawn in manual and auto scaling, and the tick's master seat too, after the quota seat check. Spawns from every swarm on the host count against the room from thirty seconds before it was granted, since a new agent takes that long to show in a reading; a room held between the load watermarks keeps its first grant time, so spawns made under it keep counting. An unknown host lets spawns pass and keeps the last known room for the next reading. An operator's own master start and restore relaunches after a reboot are not gated. |
| `agentihooks swarm <id> autoscale [--fixture <file>] [--json]` | Read only: the autoscale decision (lane ceilings, pending raise, host room and reason) for the live swarm, or for a fixture of accounts and host readings. It never refreshes quota or writes the stored decision; `applied` is false when the swarm runs manual scaling. `swarm create` stores `scaling=auto` and prints it, caps given at create included, so a new swarm's lane ceilings follow free quota seats, host room and ready work; a master keeps it on and reads this preview with `agentihooks balance` before stating capacity. In deployed mode it reads the Kubernetes worker namespace or reports why host room is off; see [Deployed host capacity](#deployed-host-capacity). |
| `agentihooks swarm <id> names [--json]` | The swarm code, its herdr space and every agent name the swarm gave, each with its type, number, session id and spawn and retire times. |
| `agentihooks swarm <id> ledger-ready` | Verify the caller's staged credential against the hosted metadata API and report readiness for every current worker and configured master seat. The active ledger address stays unchanged. |
| `agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N max-plan-agents=N` | Change the caps; `swarm <id> max-eng-agents=N` also works. |
| `agentihooks swarm <id> set masters=N` | The master seat count: `master@<id>` is the lead and answers the operator, `master-2@<id>` and up each own phases assigned by code. The tick launches a master in every empty seat after the lead. |
| `agentihooks swarm <id> set masters-step=N` | `swarm create` stores 12; a swarm created before the setting loads `0` until it is set. With no `master-per` rule the master count is one plus the unfinished engineer, CI and planner agents divided by N, masters not counted: 0 to 11 agents one master, 12 two, 24 three. The front door seat `master@<id>` never retires by scaling; backend seats are `master-2@<id>`, `master-3@<id>`, launched by the same seat launcher as the first master. A smaller count waits until it stayed lower for ten minutes, and that cool off restarts whenever the wanted count rises; a rise also withdraws the retire request of a seat wanted again. The highest backend seat retires first, as `master-per` describes below. `0` keeps the `masters=N` count, and `set masters=N` sets the step to `0` unless the same command sets it. The ledger page shows it beside the lane caps. |
| `agentihooks swarm <id> set master-per=N\|hive\|off` | Overrides `masters-step`. The master count follows the swarm size: one seat per N working agents rounded up, or one per hive (agents with no hive count as the local hive), with a floor of one; `off` (default) hands sizing back to `masters-step`. A larger swarm raises the count at once. A swarm smaller for ten minutes asks each extra master to hand off, and the count falls only past seats whose master has left. When an asked seat empties, its handoff and its other open items go to the lead and its retire request is withdrawn; until the count falls past it, items for its phases reach the lead. Each change rebalances phase owners and posts them on the masters channel. |
| `agentihooks swarm <id> set compact-limit=N` | Launch this swarm's next agents with `AGENTIHOOKS_COMPACT_LIMIT=N` (thousands of tokens); 0 keeps the default. |
| `agentihooks swarm <id> set autonomy=LEVEL` | Set how far agents go without the operator: `manual` engineers open a draft pull request and stop for the operator; `assist` engineers open a pull request and merge only after an operator approval line on the ledger; `delegate` (default) engineers queue on green checks with `agentihooks swarm <slug> merge queue <url>`; `full` is delegate, and the master also turns follow ups into tasks without asking. Agents spawned next get it in their prompt and as `AGENTIHOOKS_SWARM_AUTONOMY`; the ledger page swarm panel shows it. |
| `agentihooks swarm <id> set master-agent=claude\|codex` | Master affinity, the master lane's `agent`. The ledger page's Capacity dropdown writes the same field through Apply. When the value differs from the live master's harness, one durable inbox order reaches the master seat: hand off with Handoff v2 and stop. The tick ends the old process before it starts the successor in the same seat on the chosen harness, with the original profile and the frontier model of that harness. An unchanged value orders nothing. A failed successor launch shows on `swarm status` and the page, keeps the handoff and the seat inbox, and retries each tick. Templates, snapshots and restore carry the value. |
| `agentihooks swarm <id> set effort-min=E effort-max=E` | The effort range every lane agent starts inside, default `medium` to `xhigh`. The classifier's pick, a lane or template effort, a resume and the `init-agent` launch of a swarm lane are each clamped into it; Claude levels are `low`, `medium`, `high`, `xhigh` and `max`, and Codex levels map by rank, so Codex runs `max` as `xhigh`. Setting a lane effort outside the range is refused with the range named. The swarm panel caps row edits both ends. |
| `agentihooks swarm <id> set <gate>-gate=deny\|log only\|skip` | Operator or master only, for every gate: `identity`, `watch`, `subagents`, `reruns`, `intent`, `build`, `claim-stop`, `push-stop`, `one-push`, `claims`, `trace-plan`, `talk`, `stall`, `lane-split`, `nightwatch`. The mode lives in the swarm config; the ledger page's Gates controls beside Capacity write the same field, and `swarm status` reports every gate's current mode as `gate_modes`. Inside a swarm the ledger server, the tick, `swarm trace-plan` and the gate entry point read only the config, so an agent's `AGENTIHOOKS_GATE_<NAME>` is ignored there; it picks the mode only outside a swarm or when the swarm config cannot be read. `deny` refuses the call and logs the denial, `log only` lets the call through and logs the would-be denial, `skip` does not run the check; for `trace-plan`, `deny` refuses no call: it logs each failed plan and the second distinct one blocks the task, and `skip` still runs the trace and saves its verdict, with no failure row and no block. The old names `enforce`, `observe` and `off` remain aliases. Quote a mode containing spaces, for example `"watch-gate=log only"`. |
| `agentihooks swarm <id> set stall-gate=deny\|log only\|skip` | Operator or master only. New swarm enforcements that stop, reopen or block agents start in `log only` mode, with their own switch in the swarm config. Check their logged actions against reality before selecting `deny` to enforce. The stall watchdog defaults to `log only`: it records each would-be task reopen and live agent stop in the swarm gate log, preserving the agent, claim and task state. `deny` enables those mutations; `skip` suppresses them without logging attempts. Watchdog alerts and escalation remain active in every mode. `swarm status` reports `stall` with the other gate modes. Cases, limits, the release wait and troubleshooting: [Stall watchdog](#stall-watchdog). |
| `agentihooks swarm <id> set nightwatch-gate=deny\|log only\|skip` | Operator or master only. The night watch runs every tick after placement and needs no master: it holds the front door master seat, the spawn path and the dev merge queue against a limit. A running swarm, not asleep, whose front door seat holds no live working master for ten minutes raises an `empty master seat` finding; ready work with no agent launched for thirty minutes raises a `held spawn` finding naming the hold, `sleep`, `host room`, `cap`, `quota`, `tick` for a hold the tick recorded itself, or `no named hold`, with its evidence; no merge into dev for an hour while the queue holds entries, or the last three merge group runs failing in a row, measured whether or not the queue holds entries, raises a `merge stall` or `merge failures` finding. A stopped swarm skips the tick, so the watch rests with it. The seat and merge checks rest while the swarm is not running or sleeps; the spawn check keeps watching a sleeping swarm and names `sleep` as the hold, because a sleeping swarm spawns nothing for ready work. A pass more than five minutes after the last one closes its alerts and starts every episode over, and a merge observation that fails leaves the merge episode and its alert as they were. The findings show in every mode. It defaults to `log only`, recording each would-be action in the swarm gate log; `deny` acts: it respawns the front door master at most three times an hour, ten minutes apart, counted the same way in `log only`, past the host hold and a spent startup retry, and raises one ledger alert per episode, source `seat`, `spawn` or `merge`, closed when the condition clears; an episode logged before the switch to `deny` raises its alert on the next pass, and a second merge finding in the same episode replaces the merge alert; a merge alert also emails the operator at `AGENTIHOOKS_OPERATOR_EMAIL`, else the swarm repository's git user email, through the SMTP settings. `skip` runs the checks for their findings only, with no gate log row, action or alert. |
| `agentihooks swarm <id> set lane-split-gate=deny\|log only\|skip` | Operator or master only. The lane split moves one seat between the eng and ci lanes after the bottleneck report names the same stage three ticks running, and the `seat_wait` remedy moves one from ci to eng. It defaults to `log only`, recording each would-be move in the swarm gate log; `deny` lets it move seats, `skip` holds every move without logging. In every mode it holds a move into a lane with no ready task, and it never moves caps the operator or the master set with `max-eng-agents` or `max-ci-agents`, through `swarm set`, the ledger page or `create` flags; those caps stay where they set them for the swarm's life, and a swarm stored before the pin existed counts as set. A tick hold names its reason in the tick's held line; a remedy hold names it in the task proposal to the master. |
| `agentihooks swarm <id> set talk-gate=deny\|log only\|skip` | Operator or master only. The ledger server counts each eng and ci agent's talk writes (comments, chat lines, follow-ups) since its last outcome: a pushed commit, an opened pull request, resolved checks, a task state, pull request or proof change. Past 10, `deny` refuses the write and `log only` (the default) logs the would-be deny in the gate log. Talk owed to the operator and a typed `lift the talk gate` pass. The ledger hook's nudge and Stop gate also restart on an outcome and skip tool calls made inside sub-agents. |
| `agentihooks swarm <id> set intent-gate=deny\|log only\|skip` | Operator or master only. `swarm pr` writes the parent intent (project overview, phase, task) into the pull request body and arms the task's intent check. On the next tick the classifier reads that intent with the pull request title, body, changed files, the task proof and the task's plan slice, resolved the way `agentihooks plan read` resolves it (phase range, plan artifact or Swarm v2 package, without the shared sections), and judges whether the phase can use the change as delivered and whether it weakens anything the phase builds; under 0.3, or a weakening at 0.5 or more, the verdict is fail, with reasons. It also asks phase fit on its own, so a fail names its criterion: a change that fits the phase but is not usable yet fails on delivery, never as a phase mismatch. Each verdict records its stage, criterion, model and definition digest. `python -m scripts.gates.intent_calibration` measures the check on its corpus of retained and control inputs. `deny` returns a failed task to its agent (claimed again, a ledger comment and an inbox item); `log only` (the default) logs the fail in the gate log; `skip` skips the check. The `intent` gate (`python -m scripts.gates intent`) refuses `gh pr merge` and `swarm done` while the verdict is fail, or pending for under two minutes; a check still pending after two minutes passes unchecked and is counted. A pass names the plan lines it judged. A task whose plan passed intent at `swarm trace-plan` opens its pull request on that pass, with no pending hold, and the tick still judges the pull request and replaces the plan verdict. Running `swarm pr` again starts a new check. A split task's halves are judged and merged one by one; see [Intent grading of each half](#intent-grading-of-each-half). |
| `agentihooks swarm <id> master move --to HIVE` | Orders the live master to hand off so its successor starts on that hive. It refuses the hive the master already runs on, a hive that is not live, and a hive hive placement would not give the master. One durable inbox order reaches the master seat; after the master's Handoff v2 the tick starts the successor through hive launch on the named hive. A master that leaves without handing off gets no pin. Walkthrough: [Master failover and master move](hives.md#master-failover-and-master-move). |
| `agentihooks swarm <id> set local-max-agents=N` | With hive placement on, an eng, ci, plan or dispatch spawn no hive takes runs in the local machine's herdr panes while the machine runs fewer than N worker agents across every swarm. Default 8. See [Hive placement for one swarm on a local machine](hives.md#hive-placement-for-one-swarm-on-a-local-machine). |
| `agentihooks swarm <id> set master-move-grace=SECONDS` | How long the hive running the master may stay without a heartbeat before the tick moves the master. Default 300. Past it, and while the runtime does not see the master live, the tick records a recovery handoff that names its unknowns, bumps the master seat generation, fences the old execution on that hive and starts the successor on a live hive chosen by hive placement, or unpinned when no hive qualifies. A hive that never beat, or whose record is gone, never moves its master. A returning hive terminates each fenced execution by its recorded process group before it writes its heartbeat, and takes no launch orders while one still runs. See [Automatic failover](hives.md#automatic-failover). |
| `agentihooks swarm <id> set snapshot-minutes=N` | While the swarm runs, a background job the tick starts writes an automatic snapshot every N minutes to `~/.agentihooks/swarm/<id>/snapshots/auto-<ms>.json` and removes the oldest past ten. Default 30; a swarm without the setting takes `AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES`; 0 turns automatic snapshots off. `snapshot.json` from `snapshot` and `stop` is separate and never pruned. |
| `agentihooks swarm <id> set eng-agent=codex eng-model=M eng-effort=E eng-kind=K eng-role=TEXT` | Change one lane field (`ci-` likewise); the next spawn in that lane uses it. |
| `agentihooks swarm <id> save-template NAME` | Write this swarm's caps, compact limit and lane map as the user template NAME. |
| `agentihooks swarm <id> send-message TEXT` | Message to every live agent's inbox; never posts to chat. |
| `agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]` | The master or the operator judges a health finding: `false-positive`, `early-real`, `established`, `insufficient-evidence` or `resolved`. The finding hides for `AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES` (60); after that it shows again, with its earlier verdict attached, only when its measure is above the measure recorded at the verdict. A standing `bottleneck-stage/<stage>` finding measures the stage's latest value in the unit of the writer that owns it, not how many times it fired: task hours for the stage detector, task hours in the window for Grafana alerts, minutes from push to `Gate Required` for the ci budget pass, which counts pull request Tests runs over fifteen minutes, and for a `bottleneck-stage/langfuse_account_<account>` finding the highest value to threshold ratio across that account's error rate and turn latency p95 breaches. A stage that keeps firing while its value holds or falls stays hidden after its verdict and returns once the value rises past the one judged. For example, `bottleneck-stage/merge_queue` judged `established` at 1.5 task hours stays hidden when it fires again at 1.2 and returns when it fires at 2.0. The `Fired N times` evidence line on the finding counts firings and never brings it back. Over monitoring fires past `AGENTIHOOKS_HEALTH_WATCH_MIN` (20) watch calls since the agent's last action and over `AGENTIHOOKS_HEALTH_WATCH_RATIO` (5) per action; the master, whose job is mostly watching, gets `AGENTIHOOKS_HEALTH_MASTER_WATCH_MIN` (60) and `AGENTIHOOKS_HEALTH_MASTER_WATCH_RATIO` (15). Working on drain fires when a working agent that is not idle stays on an account the quota capacity decision marks closed, or one at or under `AGENTIHOOKS_HEALTH_DRAIN_LEFT` (10) percent routing left, the quota balancer's drain line where the tick may still place work, over `AGENTIHOOKS_HEALTH_DRAIN_MINUTES` (10) after its early quota handoff warning. The `watch` gate (`python -m scripts.gates watch`) refuses the watch call that would cross the same limits, so this finding means the gate leaked; a ledger watch re-arm with no live watcher always passes. |
| `swarm tick` Grafana alerts | See [Grafana bottleneck alerts](../reference/grafana-bottleneck-alerts.md) for setup, an example and troubleshooting. Each scheduled tick reads the swarm bottleneck alert state history from Grafana through the gateway Grafana tools, with the gateway key in `MCP_KEY_GATEWAY` at the gateway endpoint in `LANGFUSE_MCP_URL`, the same encrypted path the [Langfuse summary pass](../reference/observability-langfuse.md#the-swarm-langfuse-summary-pass) uses; with either unset it reads nothing. It asks `grafana-get_annotations` for the alert state annotations of the last day, at most 1000, bounded at ten seconds; a failed read changes nothing. Nothing listens for Grafana pushes: the swarm timer install disables and deletes the old `agentihooks-alert-intake` user service. For each known `stage` on a `swarm=bottleneck` alert whose `ledger` label names this ledger, the latest transition decides: `Alerting` with a `threshold_hours` label and an `hours` value keeps one standing `bottleneck-stage/<stage>` health finding, its hours from that transition and each new firing, keyed by its time, counted once; `Normal` clears the finding its alert raised, never one in another unit; any other state changes nothing. Only the labels inside the braces and the `hours` value are read, never the alert text. The ci budget pass alone owns the `ci_overrun` finding, which the alert read ignores: it updates that finding for each Tests run over fifteen minutes instead of adding a follow up per run. A standing finding not updated for six hours stops showing. The rules live in antoncore's Grafana provisioning. Build, ci run and launch are productive work: the alert read ignores their alerts and the stage detector never raises a finding for them, though status still ranks them. The detector opens a finding only when a stage's task hours in its window pass that stage's threshold in `stage_detector.THRESHOLD_HOURS` (seat wait and review 4, quota and host hold 2, merge queue, intent fix and ci queue 1, ci overrun and launch failure 0.25). A task's claim wait starts at the latest event that made it claimable: its add, reopen or return to scope, the unfreeze or focus that admits it, its phase opening, or a dependency done. It is split into `seat_wait`, ready while every seat of its lane was busy, counting at most as many waiting tasks as the lane has seats in ready order, and `queue_wait`, every other ready instant, which raises no finding. The detector clears a finding it opened once the stage falls back under it; an alert's finding is left to its alert. Each tick, every standing stage finding over a nonzero threshold runs its remedy once per four hour window. At `delegate` or `full`, `seat_wait` moves one seat from the ci lane to the eng lane under the lane split's caps, `launch_failure` moves the first eng or ci lane pinned to the one harness the detector saw fail to the other harness and `quota_hold` does the same for the one harness its finding names as held, which each host sample records as `held_harness` when every quota held lane is pinned to that harness; a stage whose remedy is refused or has none raises its open task titled `Bottleneck fix for the <stage> stage` to `urgent`, which the dispatcher queue orders first, and an urgent one needs nothing more. Otherwise, and for every stage below `delegate`, the master gets one proposed task carrying the hours: to add that task, or to raise the open one. Each action goes to the gate log as rule `bottleneck-remedy` with the hours it targeted and is kept as the stage's last action. |
| `agentihooks swarm <id> classify EXECUTION RULING --reason TEXT` | The master or the operator rules `lost` or `working` on an execution attempt the observer flagged for the operator under `AGENTIHOOKS_OBSERVATION_MODE=conservative`. The reason, author and time are kept on its observation. `lost` holds the attempt lost and writes the audit record; `working` holds it working until a newer loss reading (pod gone or ended, supervisor exited) flags it again, or it recovers on its own. Only a registered master record or the operator acts: any other name, an empty reason or an attempt that is not flagged is refused. `verdict` follows the same authority rule. |
| `agentihooks swarm <id> terminal EXECUTION` | For a local managed Codex execution, reopen a native viewer on its recorded thread; see [Managed Codex terminals](../reference/codex-managed-terminal.md). For Kubernetes, the operator opens the worker terminal in herdr. The command takes only the swarm and the execution: the host, the generation and the host key come from the record the controller wrote when it launched the Pod. It signs a short lived certificate with the user CA key named by `AGENTIHOOKS_TERMINAL_CA_KEY`, reads the worker namespace from the Pod policy in `AGENTIHOOKS_POD_POLICY_FILE`, writes the SSH material under the operator's own home and saves the machine in herdr. The Kubernetes path refuses a master or any agent session, and an execution with no recorded host key. |
| `agentihooks swarm-terminal setup [--antoncore DIR]`, `agentihooks swarm-terminal install` | Places the files the terminal command needs on the operator machine, with nobody copying them by hand. `setup` reads the user CA key from OpenBao `secret/agentihooks-swarm/terminal-attach` field `user_ca` with `OPENBAO_URL` and `OPENBAO_TOKEN`, and the Pod policy from the `swarm-pod-policy` ConfigMap in the antoncore checkout (`--antoncore`, else `AGENTIHOOKS_ANTONCORE_DIR`, else `~/dev/tcc-ecosystem/antoncore`). Each is checked before it replaces the previous file: an Ed25519 OpenSSH key, a valid policy. It writes `user_ca`, `pod-policy.json` and `terminal.env`, which names both as `AGENTIHOOKS_TERMINAL_CA_KEY` and `AGENTIHOOKS_POD_POLICY_FILE`, mode 0600 under `~/.agentihooks/swarm-terminal`, below the `*.env` autoload, and prints only their paths, the CA fingerprint and the worker namespace. `install` registers two jobs on this machine's agentihooks scheduler, recorded in `swarm-terminal/schedule.json`: `setup` every fifteen minutes with the `--redis-address` given to `install` as `AGENTIHOOKS_HIVE_REDIS_ADDRESS`, or, when none was given, the value the operator shell env files set, else the runner's own, and one herdr sync pass every fifteen seconds, whose state carries over in `swarm-terminal/sync-state.json`; it removes the old `agentihooks-swarm-terminal` timer and services, and `install --check` fails while either job is missing or an old unit remains. `sync` runs one pass by hand. The three commands refuse inside an agent session; the scheduled jobs run in the scheduler runner with the env file set the operator shell loads through its agentienv block (`~/.agentihooks/.env`, its `*.env` companions and `~/.env`) over the runner's own environment, and never print a value. While a refused setup holds its Priorities item and the terminal files were never placed, the sync job skips its pass quietly instead of refusing every fifteen seconds. `operator_setup.settings` reads `terminal.env` back for the service that uses the files. The workstation reads controller host key records through `scripts.swarm_v2.herdr.publications`, which opens `AGENTIHOOKS_HIVE_REDIS_URL`, else `AGENTIHOOKS_SWARM_REDIS_URL`, only as `rediss` with the certificate and its hostname verified, refuses a URL carrying `ssl_` options and a host resolving outside the private network, and keeps each worker's host key exactly as the controller recorded it at launch. |
| `agentihooks swarm <id> lift AGENT GATE` | The operator or the master lets one agent past one gate for an hour, the page's lift button in the Agents table runs it. It arms the lift for that agent and writes the same `lift` row to the gate log as typing `lift the <gate> gate` in the agent's pane; the hook gates and the talk gate honour it. Each agent in `status --json` lists `gates`: every gate that denied it in the last hour and whether a lift holds. Other agents are refused. |
| `agentihooks swarm <id> cover STAGE ITEM --reason TEXT` | Marks a bottleneck stage (`seat_wait`, `ci_overrun`, `merge_queue`, or a bottleneck share such as `review`) owned by an open `phases/<id>` or `tasks/<id>`, with the reason; a spoken name such as `merge queue` is accepted, an unknown name is refused with the names it takes, and a new cover on a stage replaces the old one. A cover on a share covers every stage inside it (`review` holds `review`, `merge_queue` and `intent_fix`; `quota` holds `quota_hold`; `host` holds `host_hold`), and a cover on a stage frees its share's dispatcher seat but leaves sibling stages' remedies running. A stage with a standing finding is also taken under its exact name. While that item is open the bottleneck hunter proposes no task and takes no remedy for the stage, and the bottleneck trigger for that share does not hold the dispatcher seat open. The cover lapses and is forgotten once the item is done, out of scope, or gone from a ledger that still lists others of its kind; the stage then fires normally. The operator, the master or the dispatcher writes it; a closed or missing item is refused, and so is every other agent. |
| `agentihooks swarm <id> freeze TARGET`, `agentihooks swarm <id> focus TARGET`, `agentihooks swarm <id> unfreeze TARGET` `[--reason TEXT] [--quote WORDS]` | Writes a freeze or focus record to the ledger's `freezes` collection, or clears one. A target is a node address (`plans/<id>`, `phases/<id>`, `slices/<id>`, `tasks/<id>`) or a selector `lane:<lane>` or `kind:<kind>`. A freeze holds the node and everything under it; a focus holds everything outside it. `unfreeze` on a node clears only the records on that node; records on nodes under it stay until each is cleared. The operator writes them; the master writes them only with `--quote` carrying the operator's recorded words; the dispatcher writes them only at `full` autonomy. Every other agent is refused. `status` lists each active record. A Doctor swarm also follows the ledger it watches: while that ledger has a focus, Doctor tasks outside the fix phase are held and fixes pass; a freeze there does not touch the Doctor, and its detectors keep running. |
| `agentihooks swarm <id> learned` | List every seat's learned notes, one line each: seat, number, maturity, text. |
| `agentihooks swarm <id> promote SEAT NUMBER MATURITY --reason TEXT` | Raise learned note NUMBER on SEAT (`eng-1` or `eng-1@<id>`) to a higher maturity, keeping who promoted it and why. Any agent of the swarm or the operator may promote to `insight`; only the master or the operator to `canon`. |
| `agentihooks swarm <id> retire SEAT NUMBER --reason TEXT` | Retire learned note NUMBER on SEAT so no later occupant's opening prompt carries it; the note keeps its number and records who retired it and why. Only the master or the operator. |
| `agentihooks swarm <id> culture set FILE` | Replace the swarm's culture with the file's text. Every new occupant of every seat, the master included, reads it in its priming chain. |
| `agentihooks swarm <id> culture show` | Print the swarm's culture. |
| `agentihooks swarm <id> masters-channel say TEXT`, `agentihooks swarm <id> masters-channel read [--all]` | The masters channel (`masters.<id>`): only a session holding one of the swarm's master seats posts and reads; every other caller is refused. Posts are kept in Redis with no expiry. Each `say` post leaves every other occupied master seat an information inbox item from the poster that names the post and the read command, so the other masters receive it at their next tool call; the text stays in the channel. `read` prints the posts the seat has not read and moves the seat's cursor, which its successor inherits; `--all` prints every post. `channel_publish` and `agentihooks channel publish` refuse every `masters.` name, so no post reaches the shared broadcast file. The tick posts each master count change there. |

For hive ownership configuration, targeted questions and follow ups, worker browser limits and G10 evidence, see [Two masters across two hives](../swarm-v2/two-hive-proof.md).

For the startup log and exit reason a refused worker Pod leaves after removal, see [Refused worker startup diagnostics](../swarm-v2/startup-diagnostics.md).

For grouped CI stage counts, worst times, run links and report retention, see
[Standing CI budget report](../ci-timing.md#standing-ci-budget-report).

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
| `agentihooks swarm <id> merge queue\|dequeue\|state URL` | Queue a pull request into `dev` on the merge queue at its current head, take it out of the queue, or report its state, head and queue entry as JSON. `queue` reads the required checks of the pull request's own repository from its `dev` branch rules and protection and refuses unless every one passed on the current head (`required checks on the current pull request head must pass before queueing`); it then enqueues that exact head with `expectedHeadOid`. It never compares the branch with `dev`, merges `dev` into it or updates it, whatever changed on `dev` since the checks ran: the merge group runs the unit suite on the head merged with the newest `dev` ([Delivery tiers](../reference/delivery-tiers.md#entering-the-merge-queue)). A repository with no merge queue on `dev` gets a squash merge instead, only for a ready pull request with green checks and a review decision of none or approved. A pull request already in the queue returns its state without another check or enqueue. The pull request can live in any repository the caller's `gh` reaches, such as agentibrain-kernel, and joins that repository's own queue ([Pull requests in other repositories](../reference/delivery-tiers.md#pull-requests-in-other-repositories)). It goes through `gh api graphql` and `gh api`, so any `gh` with `gh api` works; queue and dequeue run only for a swarm worker on its task's recorded pull request, never the master, and refuse a pull request into any other base. Each queue and dequeue appends a record with the queue before and after to the [queue journal](../reference/delivery-tiers.md#the-queue-journal). |
| `agentihooks swarm <id> prepare` | Set `--repositories /absolute/repo,/absolute/other` when adding a task, or `task set <task> repositories=/absolute/repo`. Use that explicit `repositories` list of local repository paths; without it, report that the swarm repository is the default. Prose repository names do not select repositories. Create or reuse one worktree per repository from its own `dev`, with a recorded continuation branch scoped to that repository. Return each worktree and branch. |
| `agentihooks swarm <id> publish --message TEXT -- FILE...` | Use the repository of the current task worktree; from a primary checkout, resolve the sole task repository or require a worktree selection when several are declared. Stage only explicit repository relative files, commit, run that repository’s `scripts.ci_prepush` gate, then push and retain its branch separately from other task repositories. The result names the gate. A repository without that module uses the named [AgentiHooks touched Python fallback](#fallback-gate-for-repositories-without-their-own): lint and format on changed lines, size and complexity on touched functions and changed files, in changed Python files since its own `origin/dev` merge base, without agentihooks baseline files or local tests. Unrelated tracked changes are refused before staging; unrelated untracked files are preserved. A failed stage stops publication and names the next command. Repeat the command after fixing a failure; an already committed head is graded again without an empty commit. Protected branches and tasks owned by another agent are refused. Tests run only in CI. |
| `agentihooks swarm <id> branch` | Record the current worktree branch on the task once it is on origin, with its repository (`branch_repo`, the origin remote without credentials), so dependent tasks can start from it. |
| `agentihooks swarm <id> review` | Start a review round on the task. The third round answers that review is closed: any finding still open gets one ruling from the other reader, then the merge proceeds. A fourth is refused with that text. Rounds spent before the task records a pull request, on its first pull request and on each later one, count on separate counters ([Review round counters](#review-round-counters)). |
| `agentihooks swarm <id> done [--pr URL] [proof flags]` | Close the task with the proof its kind needs; the swarm then closes the session. |
| `agentihooks swarm <id> merge priority URL` | The master or operator moves a green critical pull request ahead of waiting dev queue entries. Running entries stay in place. Waiting entries ahead are removed tail first and requeued in their original order after the critical request; their swarm owners receive an inbox message. Entries behind an already queued critical request stay in place. A failed insertion still attempts to restore every displaced entry and reports any restoration failure. Each reorder, deferral, replay and restore is recorded in the [queue journal](../reference/delivery-tiers.md#the-queue-journal). |
| `agentihooks swarm <id> ready DOC` | Release the seat of a task of any kind whose reviewed pull request waits only on checks and the merge queue. Refuses at manual or assist autonomy, without a recorded open pull request, or unless the worktree is clean and its head is the pull request head. Checks the document like `handoff`, records the pull request, head and worktree, marks the task `awaiting` `merge` (still `pr`), stores the handoff and ends the session; the finished agent no longer holds a seat and the tick never reopens the task as an orphan. Each tick the merge pass watches its checks like a checks wait, queues its green head as is like `merge queue`, waits on the queue like a merge wait, then writes the same done fields `done` writes, closes its group members and removes the worktree with `wt.sh done`. A task of a kind other than code or ci is not closed on the merge: it goes back to `open` with no claimant, its pull request and handoff kept and a comment that it awaits its proof, and the worktree is removed. Red checks, a closed pull request or a queue ejection reopen the task for the next engineer with the handoff and leave the worktree in place; a green ejection with no failed merge group run of its own is queued again instead ([Ready merge pass queue exits](#ready-merge-pass-queue-exits)). Five refusals GitHub returns (a GraphQL error, or a base other than `dev`) reopen it too; any other queue failure, an outage or checks still running, is retried and never counted. The intent gate holds `ready` like `done` in its enforce and coach modes; off and observe let it pass without a verdict. |
| `agentihooks swarm <id> block NOTE` | Comment the blocker, mark the task `blocked`, end the session. A dependency that is not `done` turns the block into a checked wait on that task instead, keeping the claim. Before blocking, refuses while a task worktree holds commits its remote branch lacks, naming the push to run, and records a pushed branch on the task; see [Blocking with unpushed commits](#blocking-with-unpushed-commits). |
| `agentihooks swarm <id> trace-plan` | Trace `plan.md` in your task work folder, one piece per line (`- what \| area, area \| why`), to the task, its phase and the project intent through the classifier. A piece under 0.3 is cut and, once the plan passes, filed as a follow-up. The task's latest five scope authorizations, master relays of operator words and operator or master comments starting `In scope:`, travel in the trace intent, and the classifier asks whether each covers each piece that is not a clearance; a piece covered at 0.7 is kept, its row and the report's `authorized` name the authorization and it is never filed as a cut. A new authorization retraces an unchanged plan without counting a failed plan; a follow up filed before the authorization stays filed. See [Task scope authorizations](#task-scope-authorizations) for recording scope, report examples and troubleshooting. A plan with more than half its pieces cut, or sized above one pull request at confidence 0.7, fails; the second failed plan blocks the task when `trace-plan-gate=deny` (default `log only` logs it). A piece appended to a passing plan is traced alone. Before the task's pull request opens, a plan that does not fail is also judged by the intent check as a plan, on its kept pieces and plan slice: whether the planned work serves the phase, covers the task and weakens nothing, with no demand for shipped code or proof; that verdict is printed as `intent` and logged like a pull request verdict. The verdict lands in `plan-verdict.json`; no classifier answer gives `unchecked`, counted in the gate log. The `build` gate (`python -m scripts.gates build`, on Edit, Write, MultiEdit, NotebookEdit, Serena edit tools and `git commit`) refuses an engineer's or CI agent's edit before a passing verdict for the current `plan.md`, and an edit or staged file outside the kept pieces' areas and the task territory; the work folder, `~/scratchpad` and files outside any git work tree are exempt, and an `unchecked` verdict lets edits through, counted. `build-gate` defaults to `log only`. |
| `agentihooks swarm <id> plan approve PHASE [--note TEXT]` / `plan send-back PHASE --note TEXT` | Decide the slice of a phase in review. At `manual` and `assist` autonomy the operator decides with the **Approve plan** and **Send back** buttons on the phase row, and the command refuses an agent naming them (at `assist` the master posts its recommendation as a phase comment). At `delegate` and `full` only the master decides. Approve moves the phase to building and the tick claims its tasks on the same pass. Send back reopens the plan task, records the note for the planner's steering and counts a round; when the planner finishes again the tick reopens the review. The third send back leaves the plan task done, marks the review escalated and raises a priority naming the notes; only the operator decides after that, and an operator send back reopens the plan task whatever the count. |
| `agentihooks swarm <id> wait [MINUTES] [--on checks PR_URL\|RUN_URL\|reply ITEM\|task ID] [--reason TEXT]` | Declare a wait. While it holds, the tick counts no idle tick for you, so you are neither nudged nor retired. A checked wait (`--on`) lasts until the tick sees its thing resolve (the checks finish or the pull request closes or merges, an Actions run completes, is cancelled or is missing, the inbox item closes, the task is done or blocked), or until it expires after MINUTES, or 12 hours when MINUTES is omitted. When the thing resolves the tick ends the wait and sends you an inbox item; expiry sends none. For a red Actions run the item names its conclusion and each failed, timed out or cancelled job, or says its jobs could not be read; [When a checks wait ends](#when-a-checks-wait-ends) gives the green and red rule for pull request checks. A bare wait lasts MINUTES, at most 60. The `claim-stop` gate (`python -m scripts.gates claim-stop`, run from a Stop condition) refuses an eng or ci agent's stop while it holds a claimed or pr task with work owed: its pull request merged (run `done`), checks failed (fix or block) or green (merge), or no pull request and no live wait. Pending checks pass the stop and record a checked wait on them, unless a live wait on a reply or a task already holds, which is kept with its reason. The third block in a row with no outcome between blocks the task. The `push-stop` gate, beside it in the engineer, cicd and qa roles, pushes the agent's task branches itself (any worktree `~/dev/worktrees/<repo>/<agent>[-N]` on its own branch, never `dev` or `main`), writes the branch and head as a ledger comment on the task, and refuses the stop with one fixed message, also sent to the agent's inbox, over uncommitted changes, a push origin refused, or pushed work with no pull request and no ledger line since the push. Planners, plan tasks, the master and the operator's sessions pass. The `one-push` gate, in the same roles on Bash calls running `git` or `gh`, keeps a pull request to one push: an eng or ci agent's `gh pr create` or `gh pr ready` is refused until the `standards-reader` and `spec-reader` sub-agents ran for its task (Claude sessions only) and its checkout is committed and on origin, with `--draft` open for the block path; a `git push` to the task branch is refused while the task's open pull request has checks running or waits in the merge queue, unless a check is red; a required check that has not reported yet counts as running. A reader counts once launched; the gate cannot see a review close. See [One push per pull request](#one-push-per-pull-request) for the refusals, configuration and troubleshooting. |
| `agentihooks swarm <id> progress --doing TEXT --ends-when TEXT` | Say what you are doing and when it ends: a ledger line on your task, a line in its work folder `progress.md` and an outcome on the progress signal. The tick raises a quiet flag (`gates/quiet/<agent>`, a gate log `count` row) on an eng or ci agent holding a claimed or pr task with no progress signal (last outcome, work folder progress line, start) for 30 minutes and no live checked wait. While it stands the `quiet` gate (`python -m scripts.gates quiet`, behind a bash flag test on every tool call) refuses every call except `agentihooks ledger`, `swarm` and `msg` commands; `progress` clears it. The stale claim finding reads the same quiet minutes, and each idle tick is a gate log `count` row (`idle-ticks`) for `doctor rates`. |
| `agentihooks swarm <id> handoff DOC [--recap FILE]` | Finish the session but keep the task: the next tick spawns a successor with the document in its prompt. `--recap` adds the recap (what you did, where you stopped, what you promised) to your seat; older recaps are kept. A hook asks for it when the session reaches `AGENTIHOOKS_COMPACT_LIMIT` thousand tokens (default 600). |
| `agentihooks swarm <id> park DOC` | Park a stacked task whose dependency is still open. Refuses unless the task's `branch` is on origin at the worktree head, the worktree holds no uncommitted changes or untracked files and, where the repo has issues, the task has an issue. Checks the document like `handoff`, writes `parked_on` (the open dependencies) and `stacked_base` (where the branch left the newest dependency branch; for a dependency whose `branch_repo` is another repository, fetched through that repository's recorded checkout or its URL, or whose pull request already merged into `dev`, the task's own fork from `origin/dev`, see [Parking on a dependency already merged into dev](#parking-on-a-dependency-already-merged-into-dev)), comments the ledger task with the blocker, comments the issue with the branch, records those other repositories in `parked_repos` (see [Park fetch and note across repositories](#park-fetch-and-note-across-repositories)), stores the handoff, ends the session and removes the worktree with `wt.sh done --pushed`, which keeps the pushed branch, since the next engineer cuts its own from the pushed branch. The tick reopens the task, which waits until its blockers are done. A read only task, any kind other than `code` or `ci`, parks without a branch or worktree: park writes only `parked_on`, the dependencies not yet done and, in a Doctor swarm, the code fixes it measures that carry no `applied_at`, comments the ledger task and stores the handoff carrying its recorded window. |
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

## Parking on a dependency already merged into dev

A dependency task can stay open after its pull request merged into `dev`, for
example while it waits on the operator or on a deployed proof. Its branch is
often deleted at merge. `swarm park` therefore reads the pull request of each
open dependency that carries a `branch` before it fetches that branch:

| Open dependency | Stacked base park records |
|---|---|
| `pr_url` names a pull request in state `MERGED` with base `dev` | The task's own fork from `origin/dev`. The dependency branch is never fetched, so a deleted branch does not stop the park. |
| `pr_url` names an open pull request, a closed one, or one merged into another branch, and the dependency is in the task's repository | Where the task branch left `origin/<branch>` of that dependency, as before. A deleted branch still refuses with `cannot fetch <branch>`. |
| No `pr_url` | Same as an unmerged pull request: the dependency branch is fetched. |
| `branch_repo` names another repository and the pull request is not merged into `dev` | That branch is fetched through the other repository's recorded checkout, or by URL without one, see [Park fetch and note across repositories](#park-fetch-and-note-across-repositories), and the base is the task's own fork from `origin/dev`. |
| No `branch` | The dependency adds no base. It is still listed in `parked_on`. |

The merged check comes first: a dependency merged into `dev` stacks on `dev`
whether or not its `branch_repo` names another repository. That repository is
still recorded in `parked_repos`.

When several dependencies carry a branch, park keeps the base with the most
commits behind it. A merged dependency stays in `parked_on`: the task still
waits until that dependency is done, and `swarm restack` then rebases the
task's commits from `stacked_base` onto `origin/dev` like any other parked
task.

Park reads each dependency's pull request with `gh pr view <pr_url> --json
state,baseRefName`, so `gh` must be authenticated for that repository. A
failed read refuses with `cannot read the dependency pull request` and parks
nothing; fix `gh` access or the task's `pr_url`, then run park again.

```bash
agentihooks swarm <id> park DOC
```

With one dependency, `t42`, merged into `dev` and its branch deleted, the
printed result reads like this, with `stacked_base` the commit where the task
branch left `dev`:

```json
{"task": "t57", "parked_on": ["t42"], "stacked_base": "<fork-commit-from-dev>", "parked_repos": [], "worktree_removed": "...", "next": "..."}
```

Delivered by
[pull request 3782](https://github.com/the-cloud-clockwork/agentihooks/pull/3782),
with a regression that refused with `cannot fetch eng-a: remote branch deleted`
in [a red proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38981617229)
before the change and passed in [the green proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38982011506).

## Blocking with unpushed commits

A blocked task reopens for the next engineer, who starts from the task's
recorded branch. `swarm block` therefore checks that the work it hands over is
on the remote first, as `swarm park` does, and records the branch it finds.
The block check is looser than park's: it accepts a remote branch that contains
the worktree head rather than one at exactly that head, ignores uncommitted
changes, and passes a worktree with no work beyond `origin/dev`.

The check runs after the dependency check: a dependency that is not `done`
still turns the block into a checked wait on that task and keeps the claim, and
no push is checked.

For each task repository, the task's `repositories` or else the swarm repo,
block finds the task worktree with the lookup `swarm publish` uses: a worktree
on the branch the task records for that repository, else one on the agent's
own branch name (`<agent>` or `<agent>-N`). Running block from the primary
checkout still checks the task worktree. Each worktree found is compared with
its remote branch: the local branch's upstream, else the task's recorded
branch, else the local branch name, on `origin` when no upstream is set.

| Worktree state | What block does |
|---|---|
| The remote branch exists and contains the worktree head | Records `branch`, `branch_repo` and `repository_branches` on the task, then blocks. |
| The remote branch exists and lacks commits the worktree holds | Refuses. The task stays claimed and nothing is written. |
| No remote branch, and the worktree holds commits beyond `origin/dev` | Refuses, as above. |
| No remote branch, and nothing beyond `origin/dev` | Blocks without recording a branch. |
| No task worktree in that repository | Blocks as before; that repository adds nothing to the record. |
| More than one worktree matches | Refuses with `multiple task worktrees match; record the task branch before publishing`. |

The refusal names the push to run:

```text
the worktree /home/me/dev/worktrees/agentihooks/engineer-323133-1771 holds commits origin lacks; push them with git -C /home/me/dev/worktrees/agentihooks/engineer-323133-1771 push -u origin engineer-323133-1771 before blocking
```

When the remote branch name differs from the local one, the command pushes
`HEAD:<remote branch>` to that upstream remote instead. Run the named push,
then block again:

```bash
git -C <worktree> push -u origin <branch>
agentihooks swarm <id> block "<the blocker in plain words>"
```

The recorded fields match what `swarm publish` writes: `branch` is the
worktree branch, `branch_repo` its `origin` URL with any password removed (and
the user too for an `http` or `https` URL), and `repository_branches` maps each
repository's primary checkout to its branch. For a task in several
repositories, `repository_branches` carries every pushed one, while `branch`
and `branch_repo` name the last repository pushed. The fields are written
before the task turns `blocked`, so the next engineer's prepare finds the
pushed branch. A task whose recorded branch is `dev`, `main`, `master` or `v1`
refuses with `publication requires a task branch outside the protected
branches`.

The check only compares commits. Uncommitted changes in the worktree do not
refuse a block; commit and push them first if the next engineer needs them.
It also reads the local clone without fetching: a remote branch that moved to a
commit this clone has not fetched refuses as if the worktree held unpushed
commits, and the count beyond `origin/dev` uses the last fetched `origin/dev`.
Run `git -C <worktree> fetch origin` and block again when the refusal names a
branch you already pushed.

Delivered by
[pull request 4053](https://github.com/the-cloud-clockwork/agentihooks/pull/4053):
its tests failed in [a red proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39030866219)
before the change and passed in [the green proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39031050627).

## Park fetch and note across repositories

### Fetching a dependency branch in another repository

When an open dependency's `branch_repo` names another repository and its pull
request is not merged into `dev`, park fetches that branch before it records
the task's fork from `origin/dev` as `stacked_base`. The dependency's
`repository_branches` lists the checkouts its own publish recorded. Park uses
the first of them whose `origin` matches `branch_repo` and runs:

```bash
git -C <that checkout> fetch origin <branch>
```

so the remote and credentials that checkout already uses apply, for example an
agentihooks task parked on an antoncore dependency fetches through the local
antoncore checkout. With no matching checkout, park fetches by URL as before:

```bash
git fetch -- <branch_repo> <branch>
```

A failed fetch refuses with `cannot fetch <branch> in <checkout>` or `cannot
fetch <branch> from <branch_repo>` and parks nothing. Check that the checkout's
`origin` points at the dependency repository and that `git -C <checkout> fetch
origin` works, then park again.

### Exact branch match

Park's push check reads `git ls-remote --heads origin <branch>` and keeps only
the line ending in exactly `refs/heads/<branch>`; block filters its remote the
same way. A proof branch whose name ends with the same text, such as
`diffcheck/<branch>`, is ignored, so it never makes a pushed task branch look
unpushed or pushed at the wrong head.

### The park ledger note

Park comments the ledger task with one note naming the open dependencies by
title:

```text
Parked on its branch until <title> and <title> is done. The next engineer restacks it onto dev and finishes it.
```

When that note would fail the ledger comment filter, for example over its
fifty word limit with several long titles, park writes this instead:

```text
Parked on its branch until every open dependency is done. The next engineer restacks it onto dev and finishes it.
```

The note is written before the issue comment and the task update. If the issue
comment fails, nothing is parked and park can run again: an agent's later
comment on the same task amends its earlier one unless someone commented in
between, so the task keeps one note.

Delivered by
[pull request 4053](https://github.com/the-cloud-clockwork/agentihooks/pull/4053),
with the same red and green proofs as
[Blocking with unpushed commits](#blocking-with-unpushed-commits).

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

### Continue an existing pull request with one push

A successor continuing an existing pull request publishes its commit only to
the retained pull request branch. Its newly prepared local branch is not also
pushed. This removes the second branch push that previously started another
Mutation preflight for the same commit. The `push-stop` gate uses the same
retained target when it pushes committed work at Stop.

The retained target comes from `continue_from` in the task's handoff envelope,
or its reclaim record when there is no nonempty handoff envelope. It must name
an `origin/` branch, and its local remote tracking ref must be an ancestor of
`HEAD`. The protected names `dev`, `main`, `master` and `v1` are excluded from
retained targets. A fresh continuation supplies no retained target. These are
runtime records; no new flag or environment variable enables this behavior.

For example, a successor may prepare a local branch with its own worker name
while its handoff retains the predecessor's pull request branch. Work in the
returned checkout and publish normally:

```bash
agentihooks swarm <id> prepare
cd <returned-worktree>
agentihooks swarm <id> publish \
  --message "Document continuation publication" \
  -- docs/pillars/swarm.md
```

The returned `branch` should name the predecessor's pull request branch, and
`head` should match that pull request's updated head. The local upstream also
tracks the retained branch. Keep using the existing pull request; do not push
the prepared branch separately to publish the same commit. From the repository
checkout, compare the result with GitHub; replace `PR_NUMBER` with the existing
pull request's number:

```bash
gh api 'repos/{owner}/{repo}/pulls/PR_NUMBER' \
  --jq '{branch: .head.ref, head: .head.sha}'
git rev-parse --abbrev-ref '@{upstream}'
```

If the retained ref is missing or fails ancestry, `publish` tries its other
recorded candidate before falling back to the local branch, as described
above. `push-stop` falls back directly to the worktree's own branch. Thus a
restacked successor that no longer continues the retained history may publish
a new branch instead.

Both push paths use a lease when the target differs from the local branch.
A remote branch deleted while its tracking ref remains stale is refused
rather than recreated; a changed remote ref can also reject the push. Read the
remote state and reconcile the continuation before retrying. Removing the
lease or pushing the same commit to a second branch defeats this protection.

See [continuation publication and preflight runs](../reference/mutation-gate.md#continuation-publication-and-preflight-runs)
for CI inspection and the distinction between a workflow run and mutation work.
Delivered by [pull request 4076](https://github.com/the-cloud-clockwork/agentihooks/pull/4076),
with a [successful focused regression proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39036611380)
and [successful mutation preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39037126637).
Those proofs establish the tested branch selection and gate results; inspecting
one live continuation's workflow runs is a separate operational check.

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

### Fallback gate for repositories without their own

`publish` runs the repository's own gate when the worktree holds
`scripts/ci_prepush.py` or `scripts/ci_prepush/__main__.py`. Otherwise it runs
the AgentiHooks touched Python fallback, and the publish result names it in
its `gate` field:

```json
{"gate": "AgentiHooks touched Python fallback"}
```

The fallback reads the repository's own ruff configuration for lint and
format; its size and complexity limits are fixed and ignore that
configuration. It grades against the merge base of the worktree's local
`origin/dev` ref and `HEAD`; `publish` does not fetch that ref first. It looks
only at `.py` files that the branch adds, copies, modifies, renames or changes
type and that still exist. A branch with no such file passes with
`Graded 0 changed Python files`.

The gate runs three stages over the whole set of changed files. A stage runs
only when every earlier stage found nothing:

| Stage | What fails |
|---|---|
| Lint | A `ruff check` finding whose lines meet a line the branch added or changed. Every line of a new file counts as changed; a renamed file counts only its edited lines. A syntax error fails wherever it sits in the file. |
| Format | A line that `ruff format` would rewrite, when that line is a changed line. A blank line `ruff format` wants to insert before a changed line counts. |
| Size and complexity | A touched function over 300 lines or over the complexity limits (`C901` above 12, `PLR0912` above 12 branches, `PLR0913` above 7 arguments), or a changed file over 3000 lines, unless the unit was already that large at the merge base and did not grow, or the base allowlist permits it. A function is touched when its syntax tree differs from the merge base; moving it without changing its body does not touch it. |

Changed lines come from `git diff -U0` between the merge base and the
worktree, which matches `HEAD` once `publish` has committed the listed files.
Findings on lines the branch never touched pass, so an old offender in a file
does not block every push that edits that file. For example, with
`label = f"plain"` already on dev, a branch that changes only the line above
it publishes and the result carries the fallback `gate` and the new `head`.
The same `F541` on a line the branch adds, here line 3, fails the
publication:

```text
AgentiHooks touched Python fallback for /home/me/dev/worktrees/litellm-state/eng-1: ... publish_gate.py failed: AgentiHooks touched Python fallback: /home/me/dev/worktrees/litellm-state/eng-1
mod.py:3:9: F541 f-string without any placeholders; Next: agentihooks swarm <id> publish --message ... -- mod.py
```

#### Suppressions

The fallback runs ruff with `--ignore-noqa`, matching its complexity rules, so
a `# noqa` comment does not clear a finding on a changed line. Fix the line
instead. For an intentional re-export, write `import name as name` or list the
name in `__all__`; ruff accepts both without a suppression. Old suppressed
lines outside the diff still pass, because they are not changed lines.

#### Touched functions

The size and complexity stage grades a function only when its syntax tree
differs from the same named function at the merge base. The comparison
ignores line and column positions, so reformatting, rewrapping or reindenting
a function, moving it within the file, or editing comments inside it leaves it
untouched and ungraded, however far over a limit it already is. A change to
its statements, arguments, decorators, default values or docstring touches
it, as does a change to a function nested inside it. Every function in a new
file, and a function new to a file, is touched. File length is graded for
every changed file.

Function names are qualified by their enclosing classes and functions
(`Reconciler.apply`, `outer.inner`); a repeated name in one file gets a
counter (`handler#2`).

#### Shrink only allowlist

The fallback reads `tests/SIZE_ALLOWLIST.json` from the merge base, never from
the worktree, so an entry added or raised on a branch has no effect on that
branch's push. A repository without the file has no allowlist. The file uses
the `scripts.size_limits` format: function entries are keyed
`<path>::<qualified name>`, file entries by path.

```json
{
  "scripts/reconcile.py::reconcile_unit": {"C901": 25, "PLR0912": 25},
  "scripts/reconcile.py::Reconciler.apply": {"PLR0913": 9, "function-lines": 340},
  "scripts/reconcile.py": {"file-lines": 3400}
}
```

`C901`, `PLR0912` and `PLR0913` grade the rule of that name, `function-lines`
the function length and `file-lines` the file length. A listed unit's cap is
the smaller of its listed value and its merge base value when the unit is
already over its limit at the base, and the listed value otherwise. An
allowlisted offender that is over its limit at the base can therefore stay
the same size or shrink, never grow, and raising its entry gives it no extra
room. Over its cap it fails with `grew from <cap> to <head>`.

Measured in [pull request 4082](https://github.com/the-cloud-clockwork/agentihooks/pull/4082)
on litellm-state `origin/dev` 53949d4, `scripts/reconcile.py`, a file with ten
existing complexity offenders, among them `reconcile_unit` at `C901` 25:

| Change | Result |
|---|---|
| A format only rewrite of 175 lines, including `reconcile_unit` | Passes: no function's logic changed |
| A new 8 argument function carrying `# noqa: PLR0913` | Fails: `proof_wide PLR0913 ... (8 > 7)`; the suppression is ignored |
| One extra branch planted in `reconcile_unit` | Fails: `C901` and `PLR0912` grew from 25 to 26 |

#### Reports and failures

A failed gate stops the publication. The error names the fallback and the
worktree, carries the gate's report, or ruff's own error text when ruff
writes one, and ends with the `Next:` command to repeat after the fix. Each
finding is one report line:

| Report | Meaning |
|---|---|
| `path:row:column: CODE message` | A lint finding on a changed line, or a syntax error anywhere in the file. |
| `path:row: would reformat a changed line` | `ruff format` would rewrite that changed line; `row` is the first such line. Run `ruff format path` and review the diff. |
| `path: name RULE message` | A touched function over a complexity limit. |
| `path: name exceeds 300 lines` or `path: file exceeds 3000 lines` | A touched function or changed file over its size limit. |
| `path: name lines grew from N to M`, `path: file lines grew from N to M` or `path: name RULE grew from N to M` | A function, file or complexity measure already over its limit at the merge base, or listed in the allowlist, grew above its base value or cap `N`. |
| `ruff check exited N` or `ruff format exited N` | Ruff could not run, usually an invalid ruff configuration in the repository. When ruff writes its own error, the publication error shows that text instead of this line. |

On a pass the gate records the passing head, which `publish` requires before
it pushes, and the publish result is printed without the gate's report. To see
the full report, ending in `Graded N changed Python files`, run
`python -I <agentihooks>/scripts/swarm/publish_gate.py` from the worktree with
a Python that has ruff installed. A passing manual run records the head too.
Tests never run in the fallback; they run in CI.

The changed line grading was delivered in [pull request 4116](https://github.com/the-cloud-clockwork/agentihooks/pull/4116).
Before it, a one line change to the litellm-state drift script failed on five
old findings it never touched; with it, the same change graded and passed.
The test that an old offender outside the diff passes failed before the fix
([red proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39044563913))
and passed after it
([green proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39045831977)).

## One push per pull request

In this repository the Tests workflow sets `cancel-in-progress` for
`pull_request` events, so every push to an open pull request cancels the Tests
run in progress and starts another. The `one-push` gate keeps a swarm worker's
pull request to one push in the normal case: the pull request opens only after
review closes and the reviewed work is on origin, and no push lands while its
checks still run unless one is already red.

The gate ships as the condition `pre-bash.git+bash.gh-one_push.py` in the
`engineer`, `cicd` and `qa` package roles. It runs on every Bash call whose
command runs `git` or `gh`, and acts only on calls that run `git push`,
`gh pr create` or `gh pr ready`, including inside a chain such as
`cd <checkout> && git push`. The directory it checks is the session's working
folder joined with the call's `workdir` and any `cd` or `git -C` in the command.

### Who it applies to

The gate decides only for a session pinned to a swarm task: it has a swarm and
an agent name (`AGENTIHOOKS_SWARM`, `AGENTIHOOKS_AGENT_NAME`, or the session
registry), a task (`AGENTIHOOKS_SWARM_TASK`), and its agent name is in the `eng`
or `ci` lane. The lane comes from the agent name, never the role, so a session
running the `qa` or `cicd` role is gated when it holds an `eng` or `ci` seat.
The master, planners, the operator's own sessions and any session outside a
swarm pass every call.

### Opening a pull request after review

`gh pr create` and `gh pr ready` are refused until both of these hold:

- **Both reviewers ran for the task.** The `standards-reader` and `spec-reader`
  sub-agents were each launched for the current task. The `subagents` gate
  counts a launch through the Agent or Task tool when its `name` or
  `subagent_type` is one of those two. This part applies to Claude sessions
  only (`AGENTIHOOKS_TARGET` unset or `claude`); Codex and Copilot sessions skip
  it.
- **The checkout is saved on origin.** `git status` in the checkout shows no
  modified tracked files, and `HEAD` has no commits missing from every
  `origin` remote branch. Untracked files are ignored. A folder that is not a
  git checkout passes.

A draft stays open for the block path: `gh pr create --draft` (or `-d`) and
`gh pr ready --undo` pass. The refusal names what is owed and the draft path.
The gate prints each refusal on one line; the examples here are wrapped for
width:

```text
open the pull request once review closes, so one push carries it: launch the
spec-reader sub-agent on the committed diff and close its findings; commit and
push the uncommitted changes. A draft pull request for a block stays allowed:
gh pr create --draft, then agentihooks swarm <swarm> block "<why>"
```

The usual order for a worker is therefore:

```bash
agentihooks swarm <swarm> publish --message "<commit message>" -- <files>
agentihooks swarm <swarm> review        # start each review round
# launch the standards-reader and spec-reader sub-agents, fix every finding,
# publish the fixes, repeat for at most three rounds
gh pr create --base dev --title "<title>" --body "<body>"
agentihooks swarm <swarm> pr <pull request url>
```

### Review round counters

Each `swarm review` call spends one round from a counter the task's state
chooses. Every counter allows three rounds. The third round on any counter
returns the closed review text as `notice`; a fourth call on the same counter
is refused with `task <task> gets no fourth review round.` followed by that
text.

| Task state when `review` runs | Counter spent |
|---|---|
| No pull request recorded (`pr_url` empty) | `rounds-before-pr` |
| The recorded pull request is the task's first one | `rounds` |
| A later pull request, such as the second half of a split | `rounds-<sha256 of the pull request URL>` |

The first pull request is written once, on the first `review` that sees a
recorded pull request: the first entry of `split_pulls` when the task is split,
otherwise its `pr_url`. Later calls read it back under a file lock, so two
reviews racing on a fresh pull request agree on which one is first. The first
pull request is the one the first review sees: when `swarm pr` records one
pull request and then another before any review, the second one is the first
pull request and counts as `rounds`; the replaced one spends no rounds.

Rounds spent before a pull request exists therefore never count against the
first pull request recorded later. A worker who runs three review rounds, then
opens and records its pull request with `swarm pr`, starts that pull request at
round one:

```bash
agentihooks swarm <swarm> review   # {"task": "t1", "round": 1}       rounds-before-pr
agentihooks swarm <swarm> review   # {"task": "t1", "round": 2}
agentihooks swarm <swarm> review   # {"task": "t1", "round": 3, "notice": "..."}
agentihooks swarm <swarm> review   # refused: no fourth review round
agentihooks swarm <swarm> pr https://github.com/<org>/<repo>/pull/12
agentihooks swarm <swarm> review   # {"task": "t1", "round": 1}       rounds
```

The counters live as files under
`~/.agentihooks/swarm/<swarm>/gates/reviews/counts/<task>/`, beside the
`first-pr` file. The proof loop health finding in `agentihooks swarm <swarm>
status` reads every counter of a task and judges its largest one against
`AGENTIHOOKS_HEALTH_REVIEW_ROUNDS`, so three rounds before the pull request
and one after it count as three, never four.

Rounds a task spent before the separate counter shipped sit in `rounds`, which
stays bound to the task's first pull request; only rounds started afterwards
with no pull request count apart.

### Pushing while checks run

A `git push` from a task session is refused when all of these hold:

1. The task records a pull request URL (`swarm pr`).
2. The push goes to that pull request's repository. The remote is the first
   positional argument, as a URL or a remote name, and `origin` when none is
   given. A push to another GitHub repository passes; a remote that is not a
   GitHub URL is treated like the pull request's repository.
3. The push writes the task's recorded branch. Each refspec's destination
   counts; no refspec, or `HEAD`, means the current branch. A task with no
   recorded branch skips this test.
4. The pull request is open and nothing is red: no required check failed, and
   the required gate job did not end skipped with nothing left running.
5. The pull request waits in the merge queue, or its checks are running. Checks
   run while a current workflow suite has not completed, a check result is
   pending, or a required check has not reported yet.

`git push --dry-run` (`-n`) and `git push --delete` (`-d`) always pass. A red
check lifts the refusal at once, so the agent can push its fix without waiting
for the other checks to finish. The push also passes when the task's pull
request URL is not a GitHub pull request link or GitHub cannot be read: the gate
refuses only on a pull request state it has seen.

While checks run, the refusal points to a checked wait:

```text
checks still run on <pull request url> and none is red: a push now cancels them.
Wait with agentihooks swarm <swarm> wait --on checks <pull request url>, then push
once they resolve, or as soon as a check goes red
```

```bash
agentihooks swarm <swarm> wait --on checks <pull request url>
# the tick ends the wait when the checks resolve; push then
git push
```

While the pull request waits in the merge queue, a push would drop it from the
queue, so the refusal asks for an explicit dequeue first:

```bash
agentihooks swarm <swarm> merge dequeue <pull request url>
git push
```

### Configuration

The gate's swarm mode is set by the operator or the master, on the ledger
page's Gates controls or from the command line:

```bash
agentihooks swarm <swarm> set one-push-gate=deny        # default: refuse and log
agentihooks swarm <swarm> set "one-push-gate=log only"  # let the call through, log the would-be refusal
agentihooks swarm <swarm> set one-push-gate=skip        # do not run the check
```

`swarm status` reports the mode in `gate_modes`. Inside a swarm the gate reads
only the swarm config; `AGENTIHOOKS_GATE_ONE_PUSH` picks the mode only outside a
swarm or when the config cannot be read.

To let an agent past the gate, the operator or the master runs
`agentihooks swarm <swarm> lift <agent> one-push`. When the agent's task records
a pull request, the lift belongs to the task and holds until that pull request's
head moves; the push that moves the head ends it. The command prints the
head it is bound to. A task with no pull request gets a one hour lift. The
operator can also type `lift the one-push gate` in the agent's pane, which lifts
the gate for that session for one hour.
Every refusal, logged would-be refusal and lift is a row in the swarm gate log.
A gate that raises lets the call through and records a `fail-open` row.

### Troubleshooting

| Symptom | Cause and action |
|---|---|
| `gh pr create` refused although both readers reviewed | The readers were launched under another name. Launch them with `name` or `subagent_type` set to `standards-reader` and `spec-reader`. A launch is counted only while the `subagents` gate runs: with `subagents-gate=skip` no reader is counted and every non draft open is refused, so set `one-push-gate` to `log only` or `skip` too. |
| Refused for `uncommitted changes` with nothing to commit | A tracked file changed in the checkout the command runs in. Check `git status` in the folder named by `workdir`, `cd` or `git -C`, not the session's starting folder. |
| Refused for `commits not on origin` | `HEAD` holds a commit no `origin` branch has. Publish it with `swarm publish`, then open the pull request. |
| A push is refused although the checks look finished | The gate counts as running a required check that has not reported yet, a check result still pending, and a current workflow suite that has not completed, including one still queued. Wait with `--on checks`. |
| A push to another branch is refused | The task records no branch, so every push to the pull request's repository is checked. Once the task records its branch (`swarm publish` records it; the `push-stop` gate only names it in a ledger comment), a push whose destination is another branch passes. A push to another GitHub repository always passes. |
| The gate never refuses | The session is not pinned to an `eng` or `ci` task, the mode is `skip` or `log only`, or a lift holds. Check `swarm status --json` for `gate_modes` and the agent's `gates`. |

The gate was delivered in [pull request 2116](https://github.com/the-cloud-clockwork/agentihooks/pull/2116);
[pull request 4736](https://github.com/the-cloud-clockwork/agentihooks/pull/4736)
made it count commits in the folder the command runs in.

## Promotion fixer intake

The minute tick runs the promotion watch for every swarm. The watch reads the newest finished push run of Tests on
`dev`. For a red run it files one urgent `ci` task per failed job with a new cause. When it files, or no promotion
alert is live, it raises a promotion alert on the ledger, and it sends the master an inbox item for the new tasks. The
tasks are named `promo-<run id>-<n>`. For a later green run it closes those tasks and the alert. Several swarms can
watch one repository, a watched swarm and its Doctor most often. Only one of them, the intake owner, files and closes
promotion fixer tasks for that repository. A failed check of one run is filed once, in the owner's ledger.

### Choosing the owner

Each repository has one owner record in Redis, `agentihooks:swarm:promotion-owner:<identity>`. The identity is the
SHA-256 of the repository's git common directory (`git rev-parse --path-format=absolute --git-common-dir`). The
primary checkout and all of its worktrees share one identity, and separate clones get separate ones. When `git` is
missing or the path is not a repository, the resolved checkout path is the identity. `AGENTIHOOKS_SWARM_KEY_PREFIX`
replaces the leading `agentihooks`.

Each pass works under the record's lock, `<record>:lock`, held for at most 300 seconds. A swarm that finds the lock
taken skips the pass and tries again on its next tick. Under the lock the watch elects the owner:

1. The candidates are every running swarm whose repository has the same identity. Proof swarms (`proof-…`) are
   never candidates and never run the watch.
2. While the recorded owner is still a candidate, it stays the owner, even when a swarm it would lose to starts
   again.
3. Otherwise the new owner is the first candidate by name that is not a Doctor of its linked swarm, so a watched
   swarm wins over its Doctor even when the Doctor's name sorts first. When only Doctors run, the first Doctor by
   name owns intake. When no swarm is running, the swarm running the pass takes it.
4. A new election writes `owner`, the former owner as `previous` (empty on the first election), and a
   `swarm:<owner>` field, which is never removed. The `swarm:` fields are the history of every swarm that has owned
   intake for the repository.

Every other swarm returns from the watch with no action. Its ledger gets no promotion task or alert.

### Handover

An owner that stops, pauses, drains or is removed stops being a candidate. The next pass by another running swarm on
the repository elects a new owner with no command from anyone. The new owner continues from the old one's state:

- **Handled runs.** The record's `seen` field holds the newest run the owners have handled. The new owner starts from
  the higher of that value and its own, so it does not file a run the old owner already handled again.
- **Active repairs.** Before it files, the owner collects the causes held by promotion tasks that are not done in its
  own ledger and in the ledger of every swarm in the history. A cause is the failed job and its failure text with
  hashes and numbers masked, so the same failure on a later run matches. A cause already held by such a task in a
  former owner's ledger is not filed again. That task, its claim, comments and proof stay where they are.
- **Green runs.** A green run closes every open or blocked task whose id starts with `promo-`, and the live
  promotion alerts, in the owner's ledger and in every former owner's ledger. It then releases every held cause that
  is not claimed or in `pr`, so the next red run with the same cause files a fresh task.

### Repeated observations and failed writes

The watch reads GitHub at most once a minute per swarm and handles a run only once. A run already handled, a task id
already in the ledger and a cause held by a task that is not done are all skipped, so every pass over the same red run
files nothing new.

The owner records a task's cause in its held causes before it writes the task to the ledger. When the ledger refuses
the write, the tick prints `scripts.swarm.promotion_watch.refresh skipped, the ledger refused its write: <reason>` to
its standard error: the user journal (`journalctl --user -t agentihooks-swarm`), or `swarm-tick.log` in the runtime
directory for a scheduled tick. The run is not marked handled, and the held cause is dropped on the next pass because
no live task carries it. The next pass by the owner, or by a new owner after a handover, files the same
`promo-<run id>-<n>` task once.

A run whose failed jobs all failed only at the `Check dev still points at this commit` step, with the annotation that
`dev` advanced before Sonar analysis, is still reported as superseded and files nothing. Failed jobs that lost their
runner still share one runner outage task.

### Checking the owner

Find the identity of a checkout and read its record. Like the code, the commands fall back to the resolved path when
the checkout is not a git repository or `git` is missing:

```bash
REPO=~/dev/tcc-ecosystem/agentihooks
COMMON=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$REPO")
COMMON=$(realpath "$COMMON")
ID=$(printf %s "$COMMON" | sha256sum | cut -d' ' -f1)
P=${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}
redis-cli HGETALL "$P:swarm:promotion-owner:$ID"
redis-cli TTL "$P:swarm:promotion-owner:$ID:lock"   # -2 when no pass holds the lock
```

| Field | Meaning |
|---|---|
| `owner` | The swarm that files and closes promotion fixer tasks for the repository |
| `previous` | The owner before the last election; empty after the first one |
| `swarm:<id>` | One field per swarm that has ever owned intake; former owners' ledgers are read for held causes and closed on green |
| `seen` | The newest dev push run any owner has handled |

An owner that is a Doctor while its watched swarm also runs took intake while the watched swarm was not running, and
keeps it until it leaves the running state. Its ledger holds the repository's promotion fixer tasks meanwhile.

Read the causes a swarm holds, keyed by task id, and the newest run it handled:

```bash
redis-cli HGETALL "$P:swarm:<id>:promotion:jobs"
redis-cli GET "$P:swarm:<id>:promotion:seen"
```

To count fixer records for one run across linked ledgers, list the promotion tasks in each one:

```bash
RUN=<run id>
for slug in rig-grade-swarm rig-grade-swarm-doctor; do
  agentihooks ledger --slug "$slug" show | jq -r --arg run "promo-$RUN-" \
    '.tasks[] | select(.id | startswith($run)) | [.id, .state, .title] | @tsv'
done
```

A failed check of one run appears in one ledger only. A task with the same run in a second ledger was filed before
intake ownership, or by a swarm on a different repository identity.

| Symptom | Cause | Action |
|---|---|---|
| A running swarm never files promotion tasks | Another running swarm on the same repository owns intake | Read `owner` in the record. Promotion tasks and alerts for the repository live in that swarm's ledger |
| A Doctor files promotion tasks | Its watched swarm was not running when an owner was last elected, and the Doctor keeps intake while it runs | Expected. The watched swarm takes intake back only after the Doctor stops running |
| A new owner did not refile an open repair | The cause is held by a task that is not done in a former owner's ledger | Expected. Work the task in that ledger; a green run closes it there |
| Two swarms on one repository both file | Their checkouts resolve to different git common directories, for example two separate clones | Compare `git rev-parse --git-common-dir` for both `repo` settings; point both swarms at the same clone or its worktrees |
| No promotion task for a red run | The lock was held, the run was superseded before Sonar, or its causes are held by tasks that are not done | Check the lock `TTL`, whether the run's failed jobs failed only at `Check dev still points at this commit`, and `promotion:jobs` for every swarm in the history |
| The same task id appears after a ledger outage | The first write was refused and the pass was retried | Expected: the retry files the task once, and later passes skip the id because the ledger already holds it |

Delivered in [pull request 3937](https://github.com/the-cloud-clockwork/agentihooks/pull/3937), with
[issue 3917](https://github.com/the-cloud-clockwork/agentihooks/issues/3917) as the spec. Its tests run two linked
swarms on the real ledger operation schema and cover one fixer for a watched and Doctor pair, concurrent observers,
independent repositories, worktree aliases, handover after a refused write and on a newer run, and green
reconciliation across owners. A live count of fixer records per failed check, before and after the change over equal
windows, is not recorded yet.

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

### Sessions that lose their swarm identity

A hook knows a session belongs to a swarm from its environment: `AGENTIHOOKS_AGENT_NAME` names a swarm lane and
`AGENTIHOOKS_SWARM` (or the slug inside the name) names the swarm. A session can lose those variables partway
through its life. When the launch wrapper dies, the agent is reparented and `swarm_pin.unpin` strips those variables from
its later hooks. Without the session pin described below, those hooks treat the session as an ordinary one: no preparation directive, no
hard gate and no marker, however large the context grows. One tick spawned master reached 930 thousand tokens this
way before the operator shut it down.

The first time a hook sees a swarm session that already has a context usage snapshot, it writes a pin beside the
markers: `~/.agentihooks/context_usage/<session id>.swarm`, a JSON object with the swarm slug and the compact limit.

```json
{"swarm": "rig-grade-swarm", "compact_limit": 600}
```

The minute tick writes the same pin for every live agent whose pane reports a conversation, with the swarm's
configured `compact-limit` (or the default when none is set). A pin is written once and never overwritten. A later
hook in that session that finds no swarm identity reads the slug from the pin, so the session still gets the
preparation directive at the compact limit and, past the margin, the hard gate with its handoff command.

| Session | Preparation and hard gate |
|---|---|
| Swarm identity in its environment | Yes; pinned once a usage snapshot exists |
| Identity lost after a hook or the tick pinned it | Yes, from the pin |
| No identity on any hook and never pinned by the tick | No |
| Operator lane (`AGENTIHOOKS_SWARM_LANE=operator`) | No, and never pinned |
| Nested session started from a swarm agent | Not from the parent's pin: it has its own session id |

The preparation directive and the hard gate use the compact limit and margin of the hook process
(`AGENTIHOOKS_COMPACT_LIMIT`, `AGENTIHOOKS_HANDOFF_MARGIN`). The limit stored in the pin is read by the Doctor
detector below.

### Doctor context overrun detector

The Doctor's `context overrun` detector runs with the other detectors on every Doctor pass. For each agent of the
watched swarm with a recorded conversation, it reads that session's context usage snapshot and raises a finding
when the context is at or over the compact limit plus `AGENTIHOOKS_HANDOFF_MARGIN` and neither a `prepare` nor a
`recycle` marker exists for the session. That state means the recycle directive never reached the session.

The compact limit comes from the session's pin; a session with no pin uses the swarm's configured limit, else
`AGENTIHOOKS_COMPACT_LIMIT` (default 600). The finding id is `context-overrun/<agent name>`, its evidence names
the session, the context in thousands of tokens and the agent state, and its number is the context in thousands
of tokens. Measure it before and after a fix:

```bash
agentihooks doctor rig-grade-swarm measure context-overrun/master@323133-0175
```

The command prints 0 once the finding is gone.

### Troubleshooting context recycle

| Symptom | Check | Cause and action |
|---|---|---|
| A swarm agent is past the hard gate and keeps working | `ls ~/.agentihooks/context_usage/<session id>.*` | No `.prepare` or `.recycle` marker and no `.swarm` pin: the session lost its identity before any hook or tick pinned it. The `context overrun` finding reports it; ask the agent for a handoff through the inbox. |
| The snapshot reads under the limit while the pane shows more | `<session id>.json`, field `used_tokens` | The statusline has not refreshed the snapshot; the directive fires on the next hook after it does. |
| A `context overrun` finding names the wrong limit | `<session id>.swarm`, field `compact_limit` | The pin keeps the limit from when it was written; `swarm set compact-limit` reaches only agents launched after it. |
| An operator lane session gets no directive | `AGENTIHOOKS_SWARM_LANE` | Expected: operator sessions are never recycled or pinned. |

[Pull request #4024](https://github.com/the-cloud-clockwork/agentihooks/pull/4024) delivered the pin and the
detector; its red Proofs run reproduced a stripped identity master whose directive returned nothing.

## One task per agent life

An agent is spawned for one task, told that task in its opening prompt, and ends when the task is done or
blocked. The prompt walks it through a fixed order: open an issue where the repo has issues, create a worktree, red test then green,
pull request into `dev`, queue on green checks with `agentihooks swarm <slug> merge queue <url>`, then `done`. An agent that cannot finish pushes a draft pull request
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

The normal `ready` requirements still apply: delegate or full autonomy, a recorded open pull request, and a clean worktree whose head matches that pull request. No additional environment variable enables split completion; `split` and `split_pulls` supply the configuration. Register all halves before releasing completed work. `ready` does not create a missing half or its pull request.

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

On a base with a merge queue the prod lockdown guard then refuses both, since the installed gh merges past the queue; `agentihooks swarm <id> merge queue <url>` is graded the same way and queues.

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

### Ready merge pass queue exits

After `swarm ready` releases a seat, the tick's merge pass queues the pull request once its checks pass and then waits on the merge queue the way `wait --on merge` does. Each tick it reads the pull request again; when the pull request reads open and outside the queue it reads it a second time before judging, so a queue that is still settling is not mistaken for an exit. It is not judged while its checks are still running, nor, for five minutes after the pass queued it, while the pass has never seen it inside the queue. A pull request seen back in the queue, for example after the [Queue retry workflow](../reference/delivery-tiers.md#lost-runner-requeue) put it back for a lost runner, keeps the task waiting.

When the pull request is no longer in the queue, the pass decides from its state and its checks on the current head:

| Observation | Task outcome | Tick action line |
|---|---|---|
| Merged, code or ci task | The task closes like `done`, with its group members. | `task <id> done, <url> merged` |
| Merged, task of another kind | The task returns to `open` with no claimant, its pull request kept, awaiting its proof. | `task <id> awaits its proof, <url> merged` |
| Closed without merging | Reopened for the next engineer with the handoff, `awaiting` set to `fix`. | `task <id> reopened, <url> was closed without merging` |
| Left the queue with a red check | Reopened once its checks have finished, `awaiting` set to `fix`. The pass reads no merge group run and never queues the pull request again. | `task <id> reopened, <url> left the merge queue without merging` |
| Left the queue with every check green, and none of its own merge group runs failed | Queued again at the same head; the task keeps its claim and gets the comment `The swarm queued <url> again: it left the merge queue while green and no merge group run of its own failed, so the task keeps its claim.` | `queued <url> for task <id>` |
| Left the queue green, but one of its own merge group runs failed, the runs could not all be listed, or it was already queued again five times | Reopened, `awaiting` set to `fix`. | `task <id> reopened, <url> left the merge queue without merging` |
| Left the queue green while one of its own merge group runs is still running | Nothing changes; the pass tries again on the next tick. | `could not read the merge group runs of <url> for task <id>: merge group runs still running: <run urls>` |
| Left the queue green and the GitHub runs listing failed | Nothing changes; the pass tries again on the next tick. | `could not read the merge group runs of <url> for task <id>: <error>` |

A merge group run counts as the pull request's own when its branch is `gh-readonly-queue/<base>/pr-<number>-<sha>` for that pull request number and it was created at or after the second the pass queued the pull request. The pass lists at most five pages of 100 `merge_group` runs; more than that counts as not listed. A run concluded `success`, `cancelled`, `skipped` or `neutral` is clean. With no failure of its own, the same head is queued again rather than handed to another engineer.

A red exit skips that read because the failure is already on the pull request's own checks. The reopen comment reads `Reopened by the swarm: its pull request <url> left the merge queue without merging. The next engineer continues it from the handoff its engineer left.` The next engineer fixes the failing check with a new commit, waits for the pull request checks and queues it with `agentihooks swarm <id> merge queue <url>`.

To see why a released task reopened or was queued again, read the swarm's comment on the task in the ledger page, then the pull request's checks and its queue state:

```bash
gh pr checks <url>
agentihooks swarm <id> merge state <url>
```

`tests/swarm/test_ready.py` pins each row. `test_a_pull_request_that_leaves_the_queue_red_reopens_without_a_requeue` queues a pull request, lets it leave the queue red and asserts the reopen line, that no merge group run was read and that the pull request was queued only once. It was added by pull request [4733](https://github.com/the-cloud-clockwork/agentihooks/pull/4733) after the green exit test moved to the requeue path and the dev promotion [coverage ratchet](../reference/delivery-tiers.md#coverage-grading) reported the red exit reopen as a line no test ran any more ([red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39218945744)). The production code did not change.

### When a checks wait ends

A wait on a pull request's checks ends green only when every required check on the pull request's latest commit has passed. A queued, pending or cancelled required check holds the wait, and a result read from an older commit never ends it. The same judgment runs for `wait --on checks <pull request url>` and for the checks stage of the [`ready` merge pass](#ready-merge-pass-queue-exits). `swarm merge queue` applies only the head match and the branch rule confirmation of step 6.

```bash
agentihooks swarm <id> wait --on checks https://github.com/<owner>/<repo>/pull/<number>
# later, as the tick action line:
#   ended the wait of <agent>: checks on <url>, now green
# and in the agent's inbox:
#   Your wait on checks on <url>, now green has ended. Pick task <id> back up: agentihooks swarm <id> done, block, or wait on the next thing.
```

Each tick judges the wait in this order. A step that does not end it holds the wait until the next tick. A checked wait declared without MINUTES lasts 12 hours, with MINUTES it lasts MINUTES; at expiry it lapses without an inbox item.

1. **Read.** The tick reads the pull request through its per tick view. No answer from GitHub holds the wait. A pull request that is no longer open ends it with `pull request <url>, now merged` or `now closed`.
2. **Bind to the head.** The reading must come from the pull request's head commit (`headRefOid`). When the newest commit in the reading is another commit, the reading carries no head and holds the wait, so checks of an older commit cannot end it.
3. **Follow pushes.** The wait records the head it judges. When the head differs from the recorded one, for example after a push, it records the new head and holds for that tick. Results of the previous head never count.
4. **Resolve.** On a repository whose workflows declare the `Gate — Required` job, the checks are resolved when a required check is red, or when nothing is still running, the gate passed and every required check concluded `SUCCESS`, `SKIPPED` or `NEUTRAL`. A required check that is queued, `PENDING`, `EXPECTED` but not yet reported, or `CANCELLED` holds the wait even after the gate passed; a required workflow run that has not completed counts as running. A gate that ended skipped with nothing red holds it too. Only the newest run of each workflow counts, so a superseded run neither holds nor ends the wait. On a repository without the gate, the required checks, or every check when none is marked required, resolve once nothing runs and either one ended `FAILURE`, `ERROR`, `STARTUP_FAILURE` or `ACTION_REQUIRED`, or all passed with at least one `SUCCESS`; a timed out check alone does not resolve them there.
5. **Read again.** The tick reads the pull request a second time without the per tick cache and repeats steps 2 to 4 on that reading. When the head moved between the two readings, it records the new head and holds.
6. **Confirm green.** A green outcome is confirmed against the base branch's rules: the required status checks of its branch protection and its repository rulesets, read with the pull request's checks on the same head. The wait ends green only when that reading names the same head, every check the rules require has reported, and every one of them passed or was neutral. A check run from an app other than the one a rule names does not count for that rule. A GitHub error during this read holds the wait. A red outcome ends without this confirmation.

The ending outcome is one line, used in the tick action line and the inbox notice:

| Outcome | Text |
|---|---|
| Every required check passed on the head and the branch rules confirm it | `checks on <url>, now green` |
| A required check failed | `checks on <url>, now red; failed: <check names>`, naming every failing check on the head, required or not |
| A check failed and the gate ended skipped | `checks on <url>, now red; failed: <check names>; Gate — Required never passed` |
| The pull request merged or closed | `pull request <url>, now merged` or `pull request <url>, now closed` |

`swarm merge queue <url>` applies the same branch rule reading before it queues and refuses with `required checks on the current pull request head must pass before queueing`, followed by `; failed: <check names>` when any failed. The `ready` merge pass queues a pull request only after its checks end green this way.

#### Troubleshooting a checks wait

| Symptom | Cause and action |
|---|---|
| The wait holds although the gate shows green | A required check is still queued, pending, expected or was cancelled. Read `gh pr checks <url>`. Rerun a cancelled required check; the wait ends on the tick after it passes. |
| The wait holds one tick longer after a push | The head moved, so the wait recorded the new head and holds for that tick. It judges only the new head's checks from then on. |
| Every check on the pull request passed, yet the wait never ends green | The branch rules require a check the head never reports, or one reported by a different app. Compare the required contexts in the rulesets (`gh api repos/<owner>/<repo>/rules/branches/<base>`) and in classic branch protection (`gh api repos/<owner>/<repo>/branches/<base>/protection`) with the check names in `gh pr checks <url>`. The wait lapses at its expiry when nothing changes. |
| The wait holds while GitHub is unreachable | Every read that fails holds the wait; the tick tries again each minute. |

`tests/swarm/test_waits.py` pins the rule: `test_a_queued_required_check_holds_the_wait_after_the_gate_passed` holds the wait for a queued, pending, expected and cancelled required check beside a passed gate; `test_a_result_read_from_an_older_commit_never_ends_the_wait` holds it for a reading whose newest commit is not the head; `test_a_green_wait_holds_until_the_branch_rules_confirm_every_required_check` and `test_a_red_wait_ends_without_asking_the_branch_rules` cover the confirmation. `tests/swarm/test_ready.py::test_the_merge_pass_queues_only_once_a_fresh_read_confirms_green` covers the merge pass. The behavior was delivered in [pull request 4114](https://github.com/the-cloud-clockwork/agentihooks/pull/4114); its tests failed first in [run 39043964843](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39043964843).

## The minute tick

An agentihooks scheduler cron (`* * * * *`, one per machine) runs `agentihooks swarm tick` every minute. `start`
registers it and starts the scheduler runner; each tick pass re-registers it if it is gone and deletes the
`agentihooks-swarm` systemd timer and service an older install left. A minute fires a holder that takes a machine
lock, so a minute that fires while a tick still runs starts no second tick; the holder stops a tick after nine
minutes. Tick output goes to the user journal: `journalctl --user -t agentihooks-swarm`. Every swarm ticks in its own thread; while one swarm's tick runs long, a quicker swarm ticks
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
12. Start the automatic snapshot job when a snapshot is due and no job holds the swarm's snapshot lock. The job
    runs as its own detached process, writes its errors to `~/.agentihooks/swarm/<id>/snapshot-job.log`, and the
    tick returns without waiting for it.

The health pass reports `merge stall` when the dev merge queue holds entries and no pull request has merged
into dev for sixty minutes. Its evidence names the queue length, minutes since the latest dev merge and the
most common failed job among the ten most recent merge queue workflow runs. The master receives a notice
on detection and every hour while it persists, even after giving the finding a verdict. This signal only
reports the stall; it imposes no enforcement and changes nothing in the queue. GitHub observations are cached
for five minutes. A failed observation retains the previous evidence and delays further notices until a fresh
observation succeeds. An empty queue or a new dev merge resets the notice interval.

Each tick renews the controller lease for three ticks. A tick is 60 seconds unless
`AGENTIHOOKS_CONTROLLER_TICK_SECONDS` sets another interval of at least one second. Set it only on the controller
of an `agentihooks controller run` install; `swarm tick` refuses to run with it set, because the host scheduler ticks
every 60 seconds. Redis server time determines expiry. The owner comes
from `SWARM_HIVE_ID`, falling back to the hostname. Ledger and spawn writes carry the epoch; stale controllers
are refused. The tick lock prevents two ticks in the same epoch and lets a higher epoch take over.

### Tick lock holder record

The tick lock is the Redis key `tick-lock` of the swarm, set for ten minutes. A running tick extends it to ten
minutes again before every step; a rename does not extend it, so a rename that runs past ten minutes loses the lock.
Its value is a holder record, a JSON object written by whoever takes the lock:

```json
{"epoch": 7, "token": "4f1c...", "host": "<boot id>:pid:[4026531836]", "pid": 41237, "start": "8812345"}
```

`epoch` is the controller lease epoch, `token` is random per take, `host` joins the machine's boot id with the
taker's process namespace, and `pid` and `start` name the process and its start time from `/proc`. Only the holder
whose record matches exactly can extend or release the lock, so a holder that lost its lock never deletes another's.

A take succeeds when the key is empty, holds a record from another lease epoch, or holds a record of the same epoch
whose holder is dead. A holder is dead only when its `host` equals the taker's and its process is gone, a zombie, or a
different process with a reused pid (another start time). In the same epoch, a record from another host or pid
namespace, a record missing `pid` or `start`, and a bare token that is not a JSON record are never judged dead; they
block until the key expires, ten minutes after the holder's last extension. A take that races another write on the
key gets no lock; one that races a change of the controller lease fails with `the controller lease is stale`.

`agentihooks swarm tick` and `agentihooks swarm rename` take the lock the same way. Each first acquires the
controller lease for this hive (the hive id from `scripts.swarm.commands.hive_id`: the hive membership, else
`SWARM_HIVE_ID`, else the seeded registry id, else the hostname), then takes the tick lock with its own holder record,
and releases it with its own token when it ends. A tick or rename killed mid run, by `kill -9`, a closed terminal or
an out of memory kill, costs no wait: the next tick of this hive on the same machine gets the same lease epoch back
while the lease is live and sees the dead holder, or gets a new epoch after the lease expired; either way it takes the
lock at once.

A tick prints each action as `<id>: <action>` on stdout and ends normally; a rename fails with the error, and
`agentihooks swarm rename` exits 1 with it on stderr after the swarm id
(`swarm: <id>: swarm <id>: another tick or rename is running`).

| Situation at take | `swarm tick` output | `swarm rename` error |
|---|---|---|
| Another hive holds the controller lease | `the swarm belongs to another hive` | `swarm <id>: belongs to another hive` |
| A live tick or rename holds the lock in this epoch | `another tick is running` | `swarm <id>: another tick or rename is running` |
| A killed tick or rename of this hive left the lock on this machine | takes it at once | takes it at once |
| A holder on another machine, or a bare token, holds it in this epoch | `another tick is running` until the key expires | `swarm <id>: another tick or rename is running` until the key expires |

#### Release on every return

A tick pass releases the lock in its `finally` on every return: the ledger in the bin (`the ledger is in the bin,
stopped`), a ledger that is gone, a failed ledger health probe (`Ledger health probe failed`), an exception
partway through, and a normal end. A tick pass stopped at its 450 second deadline, or by `SIGTERM` from the
service, releases every lock it holds before it exits and prints `the swarm tick stopped <why>; released <n> held
tick locks`. Only a process killed outright, such as by `SIGKILL` or a service start timeout, leaves its lock
behind, and the next minute tick replaces that lock through the dead holder check.

#### Reopening a swarm from the bin

`agentihooks swarm <id> reopen` sets the swarm running and runs one tick itself. When that tick dies holding the
lock, the next minute tick on the same host takes the lock over and spawns the master; the swarm does not wait out
the ten minute expiry. A Doctor start that reopens a closed Doctor ledger goes through the same reopen path.

```bash
agentihooks swarm <id> reopen
journalctl --user -t agentihooks-swarm --since "-3 min"
agentihooks swarm <id> status
```

The journal shows the next tick's actions with the master spawn, and `status` lists the master online.

#### Troubleshooting a held tick lock

When every tick of one swarm returns `another tick is running` in a few milliseconds, read the lock record:

```bash
redis-cli GET agentihooks:swarm:<id>:tick-lock
redis-cli PTTL agentihooks:swarm:<id>:tick-lock
ps -o pid,lstart,cmd -p <pid from the record>
```

- A live process under that id, with the record's start time, is a real tick still running: wait for it.
- A record with no `host`, `pid` and `start` fields, or a `host` other than this machine's, is never replaced
  early. Update the installed agentihooks to current `dev` so later ticks write holder records; the held lock
  frees itself when its `PTTL` reaches zero.

`tests/swarm/test_cli.py` pins this behavior. `test_a_binned_swarm_reopened_by_a_tick_that_died_holding_the_lock_spawns_its_master_at_the_next_tick`
bins a swarm, checks the binned tick left no lock, reopens it through a tick that dies holding the lock, and
asserts that the next tick spawns the master and leaves no lock. Three more tests assert no lock is left after
a tick returns because its ledger is gone, fails partway through, or fails its ledger health probe. They were added by
[pull request 4098](https://github.com/the-cloud-clockwork/agentihooks/pull/4098). Its red proof ran on a plant
branch that removed the dead holder check and reproduced the observed `another tick is running` loop
([red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39042891977)).

### Tick heartbeat and stale watcher

Every tick pass that runs to its end writes the swarm's heartbeat to Redis: when the pass ended, and when each
input it decided on was last observed. The host room counts from its last capacity decision, the quota from the
freshest account observation, and the ledger revision from the pass that last saw it change. The ledger probe
writes the ledger every pass, so a revision that holds still is a write that never lands; a ledger the tick cannot
read ends the pass early, which shows as no completed pass. An input never observed is recorded as unknown and
never judged.

The tick is stale when no pass has completed for 10 minutes, or when an input it decided on was 10 minutes old at
its last pass. The tick never judges itself: `agentihooks swarm tick-watch`, a process apart from the tick that
never runs a tick pass, checks every running, paused, stopping or drained swarm once a minute.

- **Alerts.** A stale episode sends one information item from `swarm` through the Redis inbox to the front door
  master seat and every live agent seat, carrying the evidence; a seat that comes live during the episode gets the
  current evidence. When a fresh pass completes, every alerted seat gets one resolved notice; a swarm stopped mid
  episode ends it with a notice that its tick is no longer watched. A restarted watcher judges an existing
  heartbeat from its own time, so it alerts at once on a tick already stale.
- **Scheduler restart.** On a workstation install (`AGENTIHOOKS_DEPLOYMENT` unset or `local`) the watcher restarts
  the scheduler tick while it is stale, at most once per 10 minutes for the whole machine: it stops the running
  scheduler supervisor, which runs the code it started from, and registers the tick again from the installed
  agentihooks the watcher runs.
- **Master respawn.** On a workstation the watcher also restarts an empty front door master while the tick is
  stale: under this hive's controller lease and the tick lock it drops lead master records whose session is gone
  and spawns the master as the tick would. A swarm that another hive ticks is left alone.
- **Master placement.** In a cluster (`AGENTIHOOKS_DEPLOYMENT` set to anything but `local`, or unset with `LEDGER_URL` set) the
  watcher places a front door master while the tick is stale, when that seat is empty and no master start is
  pending: under this hive's controller lease and the tick lock, through the cluster deployment, as the tick would.
  It drops no master record and restarts no scheduler.

A swarm whose ledger is gone, binned or in an open ledger outage is alerted but never repaired, since neither a
restart nor a respawn can end that. The watcher runs as the `agentihooks-tick-watch` user service, installed and
enabled with the other swarm services and restarted by systemd if it exits. The controller lease keeps one tick
per swarm: while another hive holds a swarm's lease, a workstation tick of that swarm returns `the swarm belongs
to another hive`.

### Controller lease during long steps

A tick pass can run longer than its lease: the lease lasts three ticks (180 seconds by default), and a pass is
stopped only at its 450 second deadline, with 30 seconds of grace. The pass keeps the lease in two ways.

- **Before every step.** Each timed step of the pass, including every step wrapped to skip a refused ledger write,
  first renews the lease, extends the tick lock and records the swarm's last tick activity. Renewal happens only at
  step boundaries, so a single step longer than the lease still loses it.
- **During a step that can outlast the lease.** Such a step runs inside a lease heartbeat: a background thread renews
  the lease once per tick interval until the step returns. The thread stops for good when the lease is no longer
  this controller's (a higher epoch took over) and tries again at the next beat after a Redis error. Today the
  Doctor timer of a `doctor` swarm runs this way.

The automatic snapshot never runs inside the pass. Step 12 of the list above only starts the detached snapshot job
and returns, so a snapshot that takes minutes cannot cost the lease. The job takes the swarm's snapshot lock for at
most 30 minutes once it starts; a tick that finds the lock held starts no second job. Two jobs started before
either holds the lock race for it: the first to take it writes the snapshot, the other exits without one.

A lease that expires mid pass ends the pass at its next step, fenced ledger call or spawn with this journal line:

```text
rig-grade-swarm: SwarmError: the controller lease is stale
```

Find these lines and the step that ran long before them:

```bash
journalctl --user -t agentihooks-swarm --since "-12h" | grep "controller lease is stale"
journalctl --user -t agentihooks-swarm --since "-12h" | grep '"event": "swarm_tick_step"' | grep '"phase": "finished"'
```

The finished step records carry each step's name, outcome and `wall_s` duration. The step that
ran longer than the lease before the error is the one to move inside a heartbeat. Any tick step that can run longer than the lease
belongs inside the heartbeat, the way the Doctor timer does:

```python
with lease.kept(store, slug, held):
    actions += skip_refused(doctor.timer, store, slug, now_ms(), scheduled)
```

The next pass acquires a new lease with a higher epoch when no other controller holds it, so one stale pass loses
only the rest of that pass's work.

### Worker execution lease through a controller rollout

A Swarm v2 Pod worker holds its task only while it keeps its execution lease. The controller's executions API grants
the lease at admission and extends it on every heartbeat; once the lease lapses, the controller refuses every write
scoped to that execution. The heartbeat and the launch grant renewal are authenticated by the worker's launch grant,
which expires as well, so the worker renews both.

A controller rollout on Anton restarts the controller (a Recreate rollout onto the new release image), and its
endpoints answer 503 until a controller is serving again. The worker lease is sized to outlast that handover, so a
running worker keeps its task through a release:

| Clock | Value | Role in a rollout |
|---|---|---|
| Execution lease | 240 seconds (`DEFAULT_LEASE_MS` in `scripts.swarm_v2.api.executions`) | The controller lease of three ticks (180 seconds) plus one tick (60 seconds) for the successor controller to serve. |
| Heartbeat | every 15 seconds | Each acknowledged heartbeat moves the lease deadline 240 seconds past the acknowledgement. |
| Retry after a failed call | 2 seconds | A 503, an unreachable controller or a malformed answer is retried, never counted as a refusal. |
| Launch grant renewal | every 60 seconds (`CREDENTIAL_SECONDS` in `scripts.swarm_v2.worker.lease_renewal`) | When an outage starts, the last renewed grant still has at least 240 seconds of its lifetime left, the whole lease. |
| Launch grant lifetime | 300 seconds | Set when the controller issues the grant; at most 900. |

During the outage the worker retries the grant renewal every two seconds and sends no heartbeat until a renewal
succeeds. When the successor controller answers, the renewal returns a new grant, the next heartbeat is
acknowledged and the deadline moves forward again. A worker therefore survives any controller outage that ends
before 240 seconds have passed since its last acknowledged heartbeat. Before this change the lease was 60 seconds and
the grant was renewed every 120 seconds, so the lease lapsed during the roughly one minute of 503 answers and every
worker on Anton exited with each release.

A controller that stays gone still costs the worker its task. The worker checks its own deadline every second and,
240 seconds after its last acknowledged heartbeat, gives the lease up with `lease_expired`, without another call.
A refusal ends the lease at once: a 400, 401, 403, 404 or 409 answer to a heartbeat or a renewal loses it with
`heartbeat_refused_<status>` or `credential_refused_<status>`, for example a grant that expired before a renewal got
through. A heartbeat answer that names another execution or task generation loses it with `generation_changed`.

What a lost lease does depends on where the worker is:

- **During start.** The worker renews from registration through preparation. A lease lost there stops the launch
  with `admission lease lost: <reason>`.
- **Under the supervisor.** The supervisor stops the agent with `lease_lost`, drains it and exits with status 70.

#### Configuration

Neither value is a flag or an environment setting. The executions API takes a `lease_ms` argument, a positive
number of milliseconds, but the control service builds it with the default. Change the lease or the grant renewal
interval in code through a reviewed pull request, and keep two relations the test suite pins:

- The lease equals the controller lease plus one tick at the default tick: `lease.ttl_ms() + lease.tick_ms()`, which
  is 240000 with `AGENTIHOOKS_CONTROLLER_TICK_SECONDS` unset.
- The grant lifetime minus the renewal interval equals the lease: 300 minus 60 is 240 seconds.

The lease does not follow `AGENTIHOOKS_CONTROLLER_TICK_SECONDS`. A controller run with a longer tick has a longer
controller lease, and its handover can outlast the 240 second worker lease.

#### Logs and troubleshooting

The renewal thread writes one JSON line per event to the worker's standard output, each carrying the
`execution_id` and `task_generation`:

```json
{"lease": "renewal_failed", "call": "credential", "status": 503, "retry_seconds": 2.0, "execution_id": "...", "task_generation": 3}
{"lease": "credential_renewed", "expires_at": "...", "execution_id": "...", "task_generation": 3}
{"lease": "renewed", "renewal_sequence": 1792449287637, "lease_deadline_ms": 1792449527637, "execution_id": "...", "task_generation": 3}
{"lease": "lost", "reason": "lease_expired", "execution_id": "...", "task_generation": 3}
```

Read a worker's lease events across a rollout:

```bash
kubectl logs <worker-pod> -n <namespace> | grep '"lease"'
kubectl logs <worker-pod> -n <namespace> | grep '"lease": "lost"'
```

| Symptom | Check |
|---|---|
| A run of `renewal_failed` lines with status 503, then `credential_renewed` | Expected during a rollout: the worker kept its lease. |
| `lost` with `lease_expired` | The controller was unavailable for 240 seconds after the last `renewed` line. Check the controller rollout and its lease takeover. |
| `lost` with `credential_refused_401` | The grant expired before a renewal succeeded. Check that the renewal interval and grant lifetime still keep the relation above. |
| `lost` with `generation_changed` | The task moved to another attempt; the controller no longer recognises this execution as current. |
| The supervisor exits 70 | Its summary line names the reason; `lease_lost` means the renewal thread gave the lease up. |

The delivered regression tests cover a worker that keeps its lease through a 225 second renewal outage, longer than
the old 60 second lease, and a worker whose controller stays gone, which loses its lease exactly 240 seconds after
its last acknowledgement. [Pull request #4248](https://github.com/the-cloud-clockwork/agentihooks/pull/4248)
records a [red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39065609729) on the tests alone
and a [green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39067755131) with the change. The
pull request does not record a live controller rollout on Anton.

### Staged and prerequisite pull requests

A task that lands in stages, or registers a prerequisite pull request before its own, declares the scope each such pull request delivers when it records it:

```bash
agentihooks swarm <id> pr <prerequisite-pull-request-url> --prerequisite "tests that accept both gate states"
agentihooks swarm <id> pr <stage-pull-request-url> --stage "the ledger field and its validation"
agentihooks swarm <id> pr <final-pull-request-url>
```

The task keeps each declaration in `pull_scopes`, a map from the pull request URL to its kind, `stage` or `prerequisite`, and its nonempty scope. The intent check grades a declared pull request against its declared scope alone, without the whole task text or the task's plan lines, and the parent intent stamped on its body names that scope in place of the task text. A declaration needs a GitHub pull request URL. A changed declaration is judged again. A pull request recorded without `--stage` or `--prerequisite` is the task's final pull request and is graded against the whole task contract; recording a declared pull request again without either option drops its declaration. A blank scope is refused, and one pull request declares one kind.

### Proof loop health findings

A `proof loop` finding identifies a task with **more than two reruns** or
**more than three review rounds**, using the default caps. The health pass
counts started agent lives and recorded review counters; moves into pull
request state alone do not count as review rounds.

#### Which starts count

Started lives are read in start order for each task. The first started life
never counts as a rerun, even when it has an excluded restart cause. Each later
started life counts unless its recorded `after` cause excludes it:

| Recorded cause | Meaning | Counts as a rerun? |
|---|---|---|
| `watchdog` | The stall watchdog reopened the task | No |
| `park` | A continuation after `swarm park` | No |
| `master` | The last recorded task state writer was a master | No |
| Empty or absent | An ordinary restart with no recorded exclusion | Yes, after the first started life |

The controller records these causes at launch; operators do not need to edit
launch history. A failed launch that never reached `started` does not count.
Exclusions apply to the marked start, so a later ordinary restart still counts.
The current health pass also excludes a later start when its recorded pull
request head differs from the preceding start and both heads are known.

With no review counter over its cap and no change of recorded head:

| Started lives in order | Counted reruns | Default finding? |
|---|---|---|
| First life, then four parked continuations | 0 | No |
| First life, watchdog continuation, master continuation | 0 | No |
| First life, then two ordinary restarts | 2 | No |
| First life, then three ordinary restarts | 3 | Yes |
| First life marked `park`, then three ordinary restarts | 3 | Yes |

Review rounds use the **largest individual review counter** for the task,
not their sum. For example, three rounds before the pull request and one on
its first pull request give a review count of three. Four in one counter
exceed the default cap. See [review round counters](#review-round-counters)
for how those counters are separated and for the review gate's own limits.

#### Configure the caps

| Environment variable | Default | Finding condition |
|---|---|---|
| `AGENTIHOOKS_HEALTH_RERUNS` | `2` | Counted reruns strictly greater than this value |
| `AGENTIHOOKS_HEALTH_REVIEW_ROUNDS` | `3` | Largest review counter strictly greater than this value |

Values must be positive integers; zero, negative and nonnumeric values retain
the defaults. Set them in the environment of the process running the health
pass. For a manual status read with caps of four reruns and five review rounds:

```bash
AGENTIHOOKS_HEALTH_RERUNS=4 \
AGENTIHOOKS_HEALTH_REVIEW_ROUNDS=5 \
  agentihooks swarm <slug> status --json
```

Five counted reruns or six rounds in one counter would exceed these caps.
This command configures only that invocation; configure the controller's
launch environment for scheduled health passes. These health settings do not
raise the review gate's three round limit or the separate claim cap. A start
after a watchdog reopen still spends a claim even though it is excluded from
the proof loop rerun count.

#### Inspect and judge a finding

```bash
agentihooks swarm <slug> status --json
```

Read the finding's task, total starts, counted reruns and review rounds.
Total starts can be high while counted reruns remain zero. Compare launch
causes and head changes with the task's history before calling it a proof loop;
a master hold or repeated parks alone are not evidence of repeated proof work.
The master or operator records a verdict using the finding ID returned by status:

```bash
agentihooks swarm <slug> verdict 'proof-loop/<task>' false-positive \
  --note 'The retained launch history shows only parked continuations'
```

Use that verdict only when the retained evidence supports it. A verdict hides
the finding under the existing cooldown rules; it does not rewrite launch
history or change either cap. If an excluded restart is still counted, check
whether its started row retained the cause. Historical rows without a cause
are treated as ordinary starts. The delivered change has a known gap if Redis
fails between consuming a restart mark and recording the started row: the
cause can be lost. Redis outage holds prevent spawning during an outage, but
do not reconstruct a missing cause.

[Pull request #3583](https://github.com/the-cloud-clockwork/agentihooks/pull/3583)
records the restart exclusion change and its regression proofs, including a
chain consisting only of parks. Its
[green Tests run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38937553476)
validates the implementation; it does not establish a live reduction in false
findings. Comparing before and after rates requires equal windows after the
running controller has loaded the correction.

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

### Stall watchdog

Each tick, after the ready merge pass, the stall watchdog checks every task in `claimed` or `pr` state that has a
pull request, and every lane. A case past its limit sends an alert; with the `stall` gate on `deny` a pull request
case also reopens the task for the next engineer with its handoff and branch.

| Case | When it counts | Default limit | Setting |
|---|---|---|---|
| green and not queued | Checks resolved and passing, not queued, not a draft, and the task not awaiting a ready merge | 10 minutes | `AGENTIHOOKS_STALL_GREEN_MINUTES` |
| red with no push | Checks resolved and failing, not queued, not a draft, latest run not cancelled | 15 minutes | `AGENTIHOOKS_STALL_RED_MINUTES` |
| owner not live | No case above, and the claimant has no live session | 10 minutes | `AGENTIHOOKS_STALL_OWNER_MINUTES` |
| unmergeable or dropped from the queue | Conflicting, or seen in the merge queue and no longer queued, while the owner is not working on it and it is not a draft | 5 minutes | `AGENTIHOOKS_STALL_UNMERGEABLE_MINUTES` |
| ready and unclaimed while its lane is full | Ready tasks wait while every configured seat of the lane is taken, accounts show free seats and scaling is not manual; alert only, closed once the lane frees or the task is claimed | 15 minutes | `AGENTIHOOKS_STALL_UNCLAIMED_MINUTES` |

A setting takes a positive whole number of minutes; any other value keeps the default. Time inside an open
[outage](#outage-holds-and-recovery) does not count.

#### When the clock starts

The green, red and unmergeable cases count from the latest of these moments:

- for green and unmergeable, the first tick of an unbroken run of ticks that saw the case; for red, the last push
  or the moment the checks turned red, whichever is later,
- the last push to the pull request,
- the start of the owner's current session on the task,
- the moment the tick last ended the owner's wait.

A green or unmergeable mark lives only from one pass to the next: a pass that does not see the case, or skips the
task because something holds it, drops the mark, and the next sighting starts a fresh clock.

The last moment gives a woken owner the full limit. The tick ends the owner's waits, such as a wait on checks, merge
or intent, in the same pass that runs the watchdog, so without it the pass that ended an owner's wait on checks
could also reopen the task as green for ten minutes before the owner had a turn to queue it. Only a wait the tick
ends records this moment, kept for 24 hours; a wait the owner declares or ends itself leaves it unchanged. The owner
not live case counts from the first tick of an unbroken run that saw it and ignores these moments.

#### What holds the watchdog

The watchdog skips a task, for every case, while:

- the task names an `after_release` tag and its repository has no newer release (see below),
- any task it depends on is not done,
- its ready record, or its owner's declared wait, names a dependency that is not yet met.

A wait on the task's own pull request holds nothing: a green pull request still counts while its owner waits on its
checks, and the owner queues it once they pass.

#### Holding a finished pull request for the next release

A green pull request kept unqueued on purpose, for example to ship with the next release, needs a release wait;
otherwise the watchdog reads it as green and not queued. Set the release it waits after on the task:

```bash
agentihooks ledger --slug <slug> task set <task> after_release=v2.17.1
```

The value is a tag such as `v2.17.1` or `2.17.1`; any other value is refused. While the task is not done, the
watchdog skips it and no lane claims it, until `git ls-remote --tags --refs origin` in the swarm repository lists a
release tag with a higher version. Tags that are not three dotted numbers, with or without a leading `v`, are
ignored. Clear the wait with an empty value:

```bash
agentihooks ledger --slug <slug> task set <task> after_release=
```

A tag lookup that fails, exits non zero or runs past 30 seconds lists no tags, so every waiting task stays held.
So that a held task never stays held silently, a running tick raises one ledger alert to the master when it finds
a task with `after_release` set, no open alert from source `release` and a failed lookup:

```text
The release tag lookup for /home/me/dev/project failed: git ls-remote exited 128. Tasks t834 stay held for a newer release until it works.
```

No second release alert is raised while one stays open. Close it once the lookup works again, for example after
restoring the repository's `origin` remote or network access.

#### Alerts and escalation

Every case past its limit sends an inbox item from `swarm` to the master seat that owns the task, naming the task,
case, minutes, pull request, owner and action. A new alert on the same task closes the earlier one as superseded; an
alert-only case is not repeated while its earlier item is open. An alert still open two limits after it was sent
goes to the operator's Priorities and to ntfy at `AGENTIHOOKS_STALL_NTFY_URL` (default the Anton alerts topic), with
`NTFY_USER` and `NTFY_PASS` for basic authentication. The open alerts show as `stall` health findings in
`swarm status`.

With the `stall` gate on `log only`, the default, a pull request case past its limit records the would-be reopen,
and the would-be stop when the owner is live, in the swarm gate log and sends the alert only; that alert's action
reads `alert only; the lane split owns the cause`, the same text the unclaimed case uses. On `deny` it stops the
live owner, reopens the task to `open` with no claimant, and comments on the task. A task whose fresh ledger row has
moved to another state or claimant, a green or owner case whose task now awaits a ready merge, and an owner held by
an outage are left alone. See the `set stall-gate` command for the modes.

#### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A green pull request held for the next release is reopened | No `after_release` on the task | `task set <task> after_release=<current release>` |
| A task with `after_release` never resumes after the release | The new tag is not newer, or is not three dotted numbers | Check `git ls-remote --tags --refs origin` in the swarm repository against the task's tag |
| A `release` alert names held tasks | The tag lookup fails | Fix the repository's `origin` remote or network, then close the alert |
| The green limit seems to restart | A push, a new owner session, the tick ending the owner's wait, or a pass that skipped or did not see the case restarted the clock | Queue the pull request once its checks pass |

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
urgent task never skips a dependency that is not done and never takes a task another agent holds. A `tune` task never stacks on a `code` or `ci` dependency that is claimed or in pull request: it waits until that dependency is done. In a Doctor swarm it also waits until the dependency carries `applied_at`, which the Doctor's `pull-dev` intervention records on each merged fix an open tune task measures, or `ledger task set <id> applied_at=<UTC time>` records by hand; the dispatcher's queue head reads the same rule. A territory
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

## Task awaiting states

A task's `awaiting` field names what the task waits on beyond its `state`. It lives on the task record in the
ledger, separate from an agent's own state such as `awaiting-decision`, which the swarm panel shows on the agent's
card. The swarm writes these values:

| Value | Written by | Cleared when |
|---|---|---|
| `merge` | `swarm ready`, which releases the seat of a task whose reviewed pull request waits only on checks and the merge queue; the task stays `pr` | The merge pass closes the task after the merge, or reopens a task of a kind other than code or ci to `open` with the field empty, awaiting its proof; a reopen for a fix writes `fix` instead; withdrawing the ready record or a stalled claim release writes it empty |
| `fix` | The merge pass, when a released task's checks go red, its pull request closes unmerged or conflicts with dev, the queue ejects it, GitHub refuses the queue or the pull request has another merge problem; once no live agent owns the task it reopens `open` with no claimant, and the tick offers it ahead of fresh work of the same rank | The next engineer's `swarm pr` writes it empty, or `approval` at `assist` autonomy; `swarm ready` writes `merge` |
| `approval` | `swarm pr` at `assist` autonomy, so the merge waits on the operator; the task sits in Priorities while it is `pr` | A later `swarm pr` at another autonomy writes it empty; the label hides once the task is `done` |
| `verify` | The merge pass, when a task with post merge proof merges before its evidence is recorded; the task reopens `open` once no live agent owns it, and until then stays `pr` while its owner waits on the proof | The evidence is recorded and the task closes, or the master runs `task set ID awaiting=` (see [Post merge proof](../reference/post-merge-proof.md)) |
| `recovery_pending` | The Kubernetes controller, while a fenced attempt's replacement has not launched or not yet reported its recovery outcome | The replacement reports; the controller writes back the value the task held before, so a `merge` or `approval` wait returns |

The master can also set or clear the field by hand:

```bash
agentihooks ledger --slug example --as master task set t12 awaiting=verify
agentihooks ledger --slug example --as master task set t12 awaiting=
```

### On the ledger page

Each task row on the ledger page shows a non empty `awaiting` value as a label in the row meta, right after the
task state: a task in `pr` with `awaiting` set to `merge` reads `pr` then `MERGE`. Underscores and dashes
become spaces, so `recovery_pending` shows as `RECOVERY PENDING`. The label has the kind label's bold
weight, uppercase and letter spacing, in the palette's `--accent-2` colour from `palette.css`; it adds no colour of
its own.

A row shows no label when the field is empty or absent, and a `done` task never shows one, even when a stale value
remains on its record. The label is display only: it carries no control, and the value changes only through the
writers above or `task set`.

### Troubleshooting

- **A task shows `RECOVERY PENDING` for a long time.** Its fenced attempt has no launched replacement, or the
  replacement has not reported. The controller writes the prior value back on its next pass once the replacement
  reports. A controller log line `the ledger refused the recovery mark on task ID` means the ledger rejected the
  write, and the label stays until a later pass succeeds.
  How the controller fences the attempt and chooses its replacement is in
  [Fenced restore decisions](../swarm-v2/checkpoint-restore.md).
- **A task shows `FIX`.** Its released pull request went red, closed, conflicts with dev, left the queue or
  GitHub refused the queue. The next engineer continues it from the stored handoff and the existing pull request.
- **A task shows `MERGE` but its pull request merged.** The merge pass closes it on the next tick while its ready
  record is held and its engineer still claims it; otherwise the merge pass clears the label and the task stays
  `pr`. A task of a kind other than code or ci goes back to `open` instead, with a comment that it awaits its proof.
- **A task shows `VERIFY` with no engineer.** The post merge measurement window has not matured or has no maturity
  time; set `contract.after_at` or clear the field with `task set ID awaiting=`.
- **No label appears.** Check the task's `state`: a `done` task never shows one. Otherwise set the field with
  `task set`, never by writing the ledger database.

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

### Agents panel cells that overflow

A cell in the Agents panel whose text does not fit its column ends in an
ellipsis, and hovering it shows the cell's full text after the ledger page's
one second tooltip delay. The actions cell at the row's end holds buttons, not
text, and keeps their own hover text.

From a page width of 1024 pixels the table lays out at fixed shares: agent,
lane, profile, overlays, model, transport, task and state each take a set
percentage, and age, plus execution and terminal on remote swarms, share the
rest. Dragging a header edge, or focusing its grip and pressing the arrow keys,
saves column widths that fix the layout at any page width; Reset layout clears
them. Below 1024 pixels with no saved widths, columns size to their content,
nothing clips, and the panel scrolls sideways.

- **Ellipsis.** A cell's text stays on one line. Transport and promoted state
  labels run inline, so they end in the cell's ellipsis. A detail line under a
  label, such as a Remote Control detail or an execution's location, sits on
  its own line and ends in its own ellipsis. An agent name ellipsizes inside a
  narrowed agent column.
- **Hover text.** One hover text covers the whole cell, wherever the pointer
  lands: everything the cell shows, joined by spaces, with transport and
  promoted state given as their summaries.

| Column | Cell shows | Hover text |
|---|---|---|
| transport | `push` `no native terminal`, each with its dot | `push · no native terminal` |
| transport, under Remote Control | `remote control live`, its detail line, the transport labels | `remote control live Remote session with complete detail push · no native terminal` |
| state, promoted | `working` `promoted` | `working · promoted` |
| execution | the observed state, how long ago it was seen, the location line | the same words on one line, location included |
| terminal | the terminal state and its detail, `behind heartbeat` when the execution's terminal lags | the same words on one line |
| any other column | its value | its value |

The design record requires this of every ledger table, and the design system
test enforces it for every agent cell on a remote swarm at 1920 by 1080.

#### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A cell's text is cut off with no ellipsis | A label inside the cell renders as a block or flex box | Ledger page bug: keep labels in agent cells inline, and give a detail line its own ellipsis |
| Hovering part of a cell shows only that part's text | The element was added after the agent row renderer wrote the cell's hover text | Ledger page bug: build the cell inside the agent row renderer |
| A tooltip opens without the one second delay | The element uses a native browser tooltip | Ledger page bug: give it hover text through the ledger tooltip module |
| Below 1024 pixels a cell still ends in an ellipsis | Saved column widths fix the layout at every width | Reset layout clears the saved widths |
| Below 1024 pixels the panel scrolls sideways | No saved widths: columns size to their content | Expected; widen the page or narrow a column |

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

Operator ledger writes and alerts retain their oldest unrelayed revision in the ledger database's relay
journal, through the ledger repository, when neither Redis nor the outbox accepts them. A
`ledger-unrelayed.json` left by an older server is moved into the journal at server start. The next
write to that ledger retries the intervening events; the ledger watch also retries every five passes,
about ten seconds at its default interval. Stable ids
prevent duplicate inbox items. Ledger replay is limited to its retained history of 2000 events: writes
older than that history cannot be reconstructed by replay. The ledger records a notice naming the lost
revision range for the operator; the watcher and tick also report trimmed history gaps.

#### Queued ledger writes

Ledger talk uses the same outbox when the local ledger server does not answer: answers, relays, new list
items and comments with an author and no attachments queue as `ledger` records in `inbox-outbox.jsonl`. Any
other ledger write is refused while ledger records wait, with
`ledger talk queued while the server was down still waits in <outbox>; retry once it lands`. A ledger linked
to a remote server never queues here.

Every `agentihooks ledger` call replays the waiting ledger records before its own write; the swarm tick and
each inbox connection replay them too. Each record is judged by what the ledger server answers:

| Server answer on replay | Result |
|---|---|
| Applied | The record leaves the outbox. |
| No connection | The ledger lane stops; this record and every later ledger record wait for the next replay. Inbox records are not held by it. |
| 5xx or 429 | The whole ledger lane waits, up to the busy age, then the record is set aside. |
| 409 revision conflict, no expected revision in its ops | Only later records that write the same resources wait, up to the busy age. |
| 409 revision conflict, ops carry `expected_revision`, all append only | Sent once more without the expected revision, so the server applies it at the current revision. A second conflict counts as busy for those resources. |
| 409 revision conflict, ops carry `expected_revision`, any other op | Set aside at once: the revision it expected has moved and can never apply. |
| Any other refusal, or a resource that cannot be read from the record | Set aside at once with the reason. |

Append only ops are `add`, `add_item`, `relay` and `answer`. A write that reached the client before the server
went down can carry the `expected_revision` the client read then, so a queued comment on a thread that has
since moved meets this case on the first replay after the thread moves.

A record's resources are the threads, lists or items its ops write: the thread of a comment, the list of
a new item, the task or phase of a task or phase op, `members` for joins and acknowledgements. A record
held by a conflict adds its resources to the held set, so a later record that shares any of them also
waits, and the order of writes to each resource holds. Records for other items, and every inbox record,
keep replaying behind it.

| Setting | Default | Behaviour |
|---|---|---|
| `AGENTIHOOKS_LEDGER_OUTBOX_BUSY_MAX_AGE_S` | 3600 seconds | Set aside a ledger record still busy or in conflict this long after it was queued. |

A set aside record goes to `inbox-outbox.skipped.jsonl` with its reason, and the replay prints it:

```text
agentihooks msg: queued message <id> was refused on replay (the ledger server answered 409 revision conflict
on <path> since this write was queued, so its expected revision can never apply); kept in <skipped outbox>
```

A record set aside after the busy age carries `<server answer> for over <busy age> seconds
(AGENTIHOOKS_LEDGER_OUTBOX_BUSY_MAX_AGE_S)`. A set aside write was not applied: read its op in the skipped
file and write it again against the current ledger.

| Symptom | Cause | Action |
|---|---|---|
| Comments on one thread wait while other ledger writes land | A record for that thread is in revision conflict without an expected revision | Wait for the busy age, or resolve the conflict on the thread; other writes are not held. |
| A ledger write other than talk is refused while talk waits | Queued ledger records wait: the server is down, answers 5xx or 429, or a record is held by a revision conflict | Bring the ledger server back; `agentihooks swarm <id> status` shows the queued count. |
| A queued comment never appeared and the set aside count rose | Its revision moved while it waited, or the server refused it | Read the reason in the skipped file and write the comment again. |

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
successor's context. An agent stopped on purpose with `agentihooks terminate-agent` is never reopened:
the command records a deliberate exit for its swarm agent, and the tick drops that agent, refunds its
claim and returns its task to open for the next claim under the lane cap. Recovery preserves the
existing task claim. A changed claim owner or execution generation, expired claim lease or changed seat
occupant fences out the old agent, so recovery cannot resume work already taken by a successor.

### Staged compatibility before admission

A tests-first prerequisite stays unqueued until its scoped implementation and compatibility inventory are final. Keep both pull requests running CI in parallel. Include affected contracts outside the edited module, such as the publisher's queue seed dependency assertion and the promotion merge method. The owner's completion declaration attests that all planned prerequisite edits are finished; CI proves the combined candidate on the recorded heads.

Add one declaration to the body of both pull requests:

```text
<!-- staged-compatibility
{
  "complete": true,
  "prerequisite": {"url": "https://github.com/OWNER/REPO/pull/123", "head": "FULL_PREREQUISITE_HEAD"},
  "candidate": {"url": "https://github.com/OWNER/REPO/pull/124", "head": "FULL_CANDIDATE_HEAD"},
  "run": "https://github.com/OWNER/REPO/actions/runs/456",
  "contracts": ["Affected compatibility job name", "Other affected contract family job"]
}
-->
```

Use the exact successful job names from a completed CI run on the candidate head. The candidate must contain the exact prerequisite commit. Both pull requests and the run belong to the same repository. A prerequisite can enter the queue after this proof succeeds; its candidate waits until that prerequisite has merged. The prerequisite head must still match after a squash merge. If either head, the scoped implementation or the inventory changes, complete the edits, regenerate the affected proof and update both declarations before admission.

`swarm ready` refuses an incomplete declaration before releasing its owner. `merge queue` checks it again against live GitHub heads immediately before admission, including admissions by the ready merge pass and queue priority path. A missing or unsuccessful contract job, an unreadable proof, or a stale head names the missing piece. Pull requests without a declaration retain their existing admission behavior. Required checks, reviews and the queue unit suite retain their current policy; the proof adds no slow suite to the pull request tier.
