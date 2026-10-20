---
title: "Hives"
parent: The Four Pillars
nav_order: 9
permalink: /docs/pillars/hives/
---

# Hives
{: .no_toc }

A hive is one machine that runs swarm agents: a workstation, a VM or a Kubernetes pod. Each hive joins the swarm
once, beats every fifteen seconds, and takes the roles its settings allow.

1. TOC
{:toc}

---

## Joining a hive

The swarm host mints a one time code; the new machine exchanges it for its own credentials.

```bash
# On the swarm host: a code valid for fifteen minutes
agentihooks hive invite lab-vm

# On the new machine
agentihooks hive join https://swarm.example.internal:8770 <code>
agentihooks hive install          # user service that runs agentihooks hive run
```

- `hive join` writes `~/.agentihooks/hive.env` (mode 0600) with the hive id, the swarm URL, a ledger credential
  and a Redis URL for the hive's own Redis user. The join URL must be https unless it is loopback.
- The join endpoint is `agentihooks hive serve`, or the Compose `hive` service started with `--profile join`. It
  always serves TLS from `SWARM_TLS_DIR`.
- Each member gets the Redis user `hive-<id>`: it reads the swarm keyspace and writes only its own hive keys.
  A launch order's receipt and acknowledgement live under those keys. The controller reads the acknowledgement and
  records the execution as working, with the hive's process target, pane, model and effort.
  Invites and credentials sit outside what a member can read.
- `hive install` writes and enables the `agentihooks-hive` user service, which runs `hive run` and restarts five
  seconds after any exit. `hive run` publishes sessions, quota, slots and repos every fifteen seconds.
- `agentihooks hive refresh` mints a new Redis credential; `agentihooks hive revoke <id>` deletes a member's
  ledger credential and Redis user.
- `agentihooks hive list` prints one line per hive: id, `live` or `stale`, ui, roles and max agents.
  `agentihooks hive show <id>` prints the full record as JSON.

A Kubernetes worker pod joins the same way from its entrypoint; see
[Worker hives in Kubernetes](#worker-hives-in-kubernetes).

## Settings

Set them with `agentihooks hive set <id> key=value ...`, or on the ledger page's Hives panel, which writes through
`PATCH /api/v1/hives/<id>` and needs the operator.

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `name` | text | the hive id | Display name. |
| `ui` | `yes`, `no` | `no` | The machine has a screen and a person at it. |
| `ephemeral` | `yes`, `no` | `no` | The machine can disappear, such as a pod. Placement prefers lasting hives on a tie. |
| `roles` | comma list of `master`, `qa`, `frontend`, `eng`, `ci`, `plan` | none | Work the hive takes. |
| `prefer` | comma list of `role:rank` | none | Rank 1 is chosen first for that role; unranked hives come after ranked ones. |
| `max-agents` | whole number, 1 or more | `1` | Working agents the hive runs at once. Masters do not count against it. |

`master`, `qa` and `frontend` are UI roles: a hive with `ui=no` refuses them, and `prefer` refuses a role the hive
does not take.

### Roles and prefer: the operator's layout

The operator runs a PC, a VM and a set of pods:

```bash
agentihooks hive set pc  ui=yes roles=master prefer=master:1
agentihooks hive set vm  ui=yes roles=master,qa,frontend prefer=master:2,qa:1,frontend:1
agentihooks hive set pod-1 ui=no ephemeral=yes roles=eng,ci,plan max-agents=1
```

- The master runs on the PC while it is live, and on the VM when the PC is gone.
- QA and frontend work runs on the VM, the only hive that takes them.
- Engineer and CI work runs on the pods.

## Placement

Every launch places its agent on one hive. For a worker role, placement keeps the live hives that take the role,
then drops, in order, those with no room under `max-agents`, those that do not advertise the account slot the
router picked, those that do not list the task's repo, and, for an interactive login, those that do not hold that
login. Each step that empties the list names its reason, and the launch waits with it.

Among the hives left, placement picks the lowest `prefer` rank for the role, then the most room, then a lasting
hive over an ephemeral one, then the hive id.

`eng`, `ci` and `plan` run on headless hives. When a UI hive also takes one of them, placement tries the headless
hives first and takes the UI hive only when no headless hive taking the role has room or none is live. A headless
hive with room that lacks the account slot, the repo or the login makes the launch wait instead. The agent record
carries the fallback reason as `placement_fallback` in its profile decision; a task pinned to a UI hive records the
pin as its reason. UI roles take only UI hives; the master
keeps its headless fallback below. The engineer, cicd and planner
profiles place as `eng`, `ci` and `plan`; a frontend or qa profile on any lane places as its UI role.

When no live hive takes the role:

- `qa` and `frontend` wait with "no UI hive live".
- Any other role waits with "no live hive takes" the role.
- The master has its own chain, below.

A hive counts as live while its last heartbeat is under ninety seconds old. Codex launches are not placed on hives.

### Master placement

The master is placed in three tiers, each tried in `prefer` order:

1. A UI hive with the `master` role that holds the native interactive login for the master's harness.
2. A UI hive with the `master` role and a subscription or api slot for that harness.
3. A headless hive, `ui=no`, with a subscription or api slot, whatever its roles.

Every tier also needs the hive to be live and to list the repo. With no candidate the master waits with "no live
hive can run the master with interactive, subscription or api authentication".

## Headless fallback

When every UI hive is gone, the master still runs: tier three starts it on a headless hive through the same hive
launch, on a subscription or api slot. Two health findings mark the state until it ends:

- `master-headless`: the master runs on a headless hive.
- `master-fallback-auth`: the master wanted its interactive login and runs on another route, so phone access
  through Remote Control is lost.

Placement runs at every master launch, so the successor after the next handoff goes back to the best UI hive that
is live then. `agentihooks swarm <id> master move --to HIVE` forces that move without waiting: the master hands
off and its successor starts on the named hive.

## Failover timing

| Clock | Value | Effect |
|---|---|---|
| Hive heartbeat | every 15 seconds | Sessions, quota and reports expire after 45 seconds without a beat. |
| Live window | 90 seconds | A hive past it is stale and gets no new launches. |
| Minute tick | 60 seconds (`AGENTIHOOKS_CONTROLLER_TICK_SECONDS`) | Each tick checks the master's hive. |
| `master-move-grace` | 300 seconds | How long the master's hive may go without a heartbeat before the tick moves the master. |
| Controller lease | three ticks, 180 seconds by default | Another controller takes over at a higher epoch once it lapses. |
| Worker execution lease | 240 seconds | A Swarm v2 Pod worker keeps its task through a controller handover; see [worker execution lease through a controller rollout](swarm.md#worker-execution-lease-through-a-controller-rollout). |
| Launch slot reservation | 300 seconds | A started launch order not acknowledged by then is recorded as ambiguous. |

The master moves when its hive's heartbeat is older than the grace and the runtime does not see the master live,
so a lost master hive is replaced between five and six minutes after its last beat at the defaults. A hive that
never beat, or whose record is gone, never moves its master. Set the grace with
`agentihooks swarm <id> set master-move-grace=SECONDS`.

The steps of the move are in [Automatic failover](#automatic-failover).

## Master failover and master move

The master seat changes hive in two ways: the minute tick moves it off a hive that went quiet, and the operator
moves it to a hive he names. In both, the successor starts through hive launch in the same master seat. The
operator move pins it to the named hive; the automatic move pins it when a live hive qualifies.

### Automatic failover

Each tick checks the live master record. It moves the master only when all of these hold:

- The master's hive has beaten at least once and its record still exists.
- Its last heartbeat is older than `master-move-grace` seconds.
- The runtime does not see the master's session live.

A stopped swarm is checked only when an operator message is waiting for its master.

Then, in one tick:

1. It stores a recovery handoff for the master seat. The handoff says the master should continue on the swarm,
   names the hive and how many seconds ago it stopped beating, and marks Done and Stopped at as `unknown`, because
   the old master left no document. Its Read first list names the master inbox.
2. It records the transfer with the reason `hive-lost`.
3. It bumps the master seat generation and fences the old execution on the quiet hive.
4. It asks master placement for a target among the live hives and records the pending move with that hive.
5. It marks the old master record finished and logs `moved master <name> off lost hive <hive>` in the tick output.

The next master start reads the recovery handoff, pins the successor to the recorded hive and clears the pending
move once the successor is placed. When no live hive qualifies, the move records no target and the successor
starts unpinned on the normal master path.

Tune the grace per swarm:

```bash
agentihooks swarm rig-grade-swarm set master-move-grace=600
```

A hive that pauses longer than the grace, such as a laptop asleep, loses its master; a shorter grace replaces a
lost master sooner. The default is 300 seconds.

### Operator master move

```bash
agentihooks swarm rig-grade-swarm master move --to vm
```

The command checks the move before it writes anything, and refuses with these errors:

| Error | Cause |
|---|---|
| `no live master to move` | The swarm has no live master record. |
| `the master already runs on hive vm` | The target is the master's current hive. |
| `hive vm is not live` | The hive has no record, or its last heartbeat is over ninety seconds old. |
| `the master cannot move to hive vm: <reason>` | [Master placement](#master-placement) would not give the master that hive; the reason is the placement wait reason. |

On success it sends one durable inbox order from `operator` to the master seat and prints the pending move as
JSON, with `from`, `to`, `master`, `item` (the order's inbox id) and `state` set to `ordered`. The order tells the
master to finish its current step, write a Handoff v2 carrying its pending decisions, held messages, claimed task
needs, audit plan and operator overrides, run `agentihooks swarm <id> handoff <doc> --reason operator` and stop.
After that handoff, the tick starts the successor on the named hive and closes the order as done.

A second `master move` replaces the first: it cancels the earlier order in the inbox with "the master move was
replaced" and records the new target.

The successor is pinned only when its transfer follows the master the move named. A master that leaves without a
handoff, or a different master that takes the seat first, starts on normal placement and the pin is not applied.

The ledger page offers the same order: each live hive row in the Swarm tab's Hives panel carries a **move master**
button. It is disabled while another swarm control from the page is running, on a hive that is not live and on the
hive the master already runs on.

### Troubleshooting

- **The master's hive is down and the master did not move.** Check the hive's heartbeat with
  `agentihooks hive list`. A hive that never beat, or whose record was removed, never moves its master, and the move
  waits the full grace plus up to one tick. While the runtime still reports the master's session live, the tick
  leaves it alone.
- **The successor started on another hive than the one ordered.** The pin applies only to the successor of the
  master the order named. If that master left without the handoff the order asks for, its successor uses normal
  placement. Run `master move` again for the new master.
- **The old hive came back and its master is still running.** A returning hive terminates each fenced execution
  before its first heartbeat and takes no launch orders while one still runs; see [Fencing](#fencing).

## Fencing

Fencing stops a superseded agent or controller from acting after its replacement starts.

- **Seat generation.** A failover bumps the master seat's generation and records the old execution as fenced on
  its hive.
- **Returning hive.** Before it writes its first heartbeat, a hive that comes back terminates every fenced
  execution by its recorded process group. While one still runs it takes no launch orders.
- **Controller epoch.** Each controller holds a lease with an epoch. Launch orders and ledger writes carry it, and a
  write from an older epoch is refused with "the controller lease is stale". Release a lease by hand with
  `agentihooks swarm <id> controller release`.
- **Ledger source.** Moving a workstation ledger into the hosted service with `--freeze` fences the source: its
  SQLite database refuses writes with "ledger moved to hosted service" and media writes answer 409.

## Worker hives in Kubernetes

The `agentihooks-swarm` chart runs worker pods from the hive worker image. Each pod:

1. Clones the repos in `workers.repositories` under its repo root.
2. Installs a mounted credential as `hive.env`, or joins with a mounted one time code.
3. Sets itself `ui=no ephemeral=yes roles=eng,ci,plan max-agents=<workers.maxAgents>`.
4. Runs `agentihooks hive run`.

Give exactly one of `workers.joinCodeSecret` or `workers.credentialSecret`. A join code runs with a persistent
home. Credentials come from ExternalSecrets by name. The hive id is the one minted when the code was exchanged, so
replicas that mount the same credential would report the same hive id and agent limit; the chart refuses
`workers.replicas` above 1. Scale by installing one release per hive credential; the pod name is used only when no
credential is installed.

## Related pages

- [Deployment modes](../reference/deployment-modes.md): where the controller, ledger and Redis run, and the
  regulated-site recipe.
- [Swarm](swarm.md): the master, seats, the minute tick and the swarm commands.
- [Claude account load balancing](load-balancing.md): subscription and api slots.
- [Hosted ledger auth](../reference/hosted-ledger-auth.md): hive tokens and edge mode.
