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
| `roles` | comma list of `master`, `qa`, `frontend`, `eng`, `ci`, `plan`, `dispatch` | none | Work the hive takes. |
| `prefer` | comma list of `role:rank` | none | Rank 1 is chosen first for that role; unranked hives come after ranked ones. |
| `max-agents` | whole number, 1 or more | `1` | Working agents the hive runs at once. Masters do not count against it. |

`master`, `qa` and `frontend` are UI roles: a hive with `ui=no` refuses them, and `prefer` refuses a role the hive
does not take.

### Roles and prefer: the operator's layout

The operator runs a PC, a VM and a set of pods:

```bash
agentihooks hive set pc  ui=yes roles=master prefer=master:1
agentihooks hive set vm  ui=yes roles=master,qa,frontend prefer=master:2,qa:1,frontend:1
agentihooks hive set pod-1 ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1
```

- The master runs on the PC while it is live, and on the VM when the PC is gone.
- QA and frontend work runs on the VM, the only hive that takes them.
- Engineer, CI, planner and dispatcher work runs on the pods. For the PC to take the dispatcher when every pod is
  full, its roles must list `dispatch`: `agentihooks hive set pc roles=master,dispatch`.

## Placement

Every launch places its agent on one hive. For a worker role, placement keeps the live hives that take the role,
then drops, in order, those with no room under `max-agents`, those that do not advertise the account slot the
router picked, those that do not list the task's repo, and, for an interactive login, those that do not hold that
login. Each step that empties the list names its reason, and the launch waits with it.

Among the hives left, placement picks the lowest `prefer` rank for the role, then the most room, then a lasting
hive over an ephemeral one, then the hive id.

`eng`, `ci`, `plan` and `dispatch` run on headless hives. When a UI hive also takes one of them, placement tries
the headless hives first and takes the UI hive only when no headless hive taking the role has room or none is live.
A headless hive with room that lacks the account slot, the repo or the login makes the launch wait instead. The
agent record carries the fallback reason as `placement_fallback` in its profile decision; a task pinned to a UI
hive records the pin as its reason. UI roles take only UI hives; the master keeps its headless fallback below. The
engineer, cicd, planner and dispatcher profiles place as `eng`, `ci`, `plan` and `dispatch`; a frontend or qa
profile on any lane places as its UI role.

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

## Prompt and workspace transport

A hive launch runs the agent on another machine, which has no copy of the ledger and no task work folder. The
controller therefore builds everything the agent reads at start and sends it in the launch payload; the hive sends
the agent's work folder lines back every heartbeat.

### What the launch payload carries

When placement picks a hive, the controller writes the payload to
`<root>:swarm:<slug>:launch-prompt:<execution id>` (`<root>` is `AGENTIHOOKS_SWARM_KEY_PREFIX`, default
`agentihooks`). The key expires with the 300 second launch slot reservation. Next to the task fields and the
profile decision it holds:

| Field | Content |
|---|---|
| `hive_opening` | The full opening prompt, built on the controller where the ledger is, for the Claude harness. |
| `hive_opening_digest` | The sha256 hex digest of `hive_opening`. |
| `hive_seeds` | The task's work folder files: `steering.md` whole, and the last 65,536 characters of `progress.md` and `proof.md`. Empty when the task has no work folder. |

The opening names the work folder as `~/.agentihooks/swarm/<slug>/tasks/<task id>`, so the same text is correct
below whichever home the hive runs the agent in.

### What the hive does with it

An order the hive picks up after its 300 seconds is acknowledged as failed with the reason `expired` before the
payload is read. Otherwise the hive checks the payload and acknowledges the order as failed with the reason `prompt`
when:

- the payload key is missing;
- the payload is not JSON, or its task id differs from the order's;
- `hive_opening` is missing or not text, or `hive_opening_digest` does not match it.

The controller ends the execution, drops the launch order and reports the spawn refused with
`hive launch failed: <reason>`.

A payload that passes is written below the hive's own home, in `~/.agentihooks/swarm/<slug>/tasks/<task id>`:

- `steering.md` is replaced with the controller's copy on every launch.
- `progress.md` and `proof.md` are written only when the hive does not hold them yet. On a relaunch of the same task
  on the same hive, its own lines stay and the controller's copy is not merged in.

The agent then starts with `hive_opening` exactly as carried. The hive does not rebuild the prompt from the task.

### Work folder lines on the ledger page

On every fifteen second heartbeat, each hive writes the work folder lines of the executions it hosts to
`<root>:hive:<hive id>:workspaces`, expiring after 45 seconds. Each entry holds `latest_progress` and
`latest_proof`: the last three non-blank lines of `progress.md` and `proof.md`, read from the last 4,096 bytes of
each. A key is left out when its file has no such line. The copy covers every swarm task whose latest execution,
the live one or else the one started last, is a worker on that hive. Masters are left out.

When the controller publishes swarm status, each task with a work folder takes its lines from the hive that runs,
or last ran, its latest execution. A task whose latest execution ran on the controller's own machine is read from
the local folder. These lines are what the ledger page shows in the task's Contract and proof fold.

### Resume and recovery prompts

The restore prompt after a reboot and the resume prompt after an outage tell the agent to re-read its task folder.
For an agent on a remote hive both prompts name `~/.agentihooks/swarm/<slug>/tasks/<task id>`, the folder below the
hive's home, instead of the controller's absolute path.

### Upgrading

A hive refuses a launch payload without `hive_opening` and its digest, so an older controller cannot launch on a
newer hive. Deploy the controller first, then the hives.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Launches on one hive end with `hive launch failed: prompt` | The controller predates prompt transport and sends no opening and digest. | Upgrade the controller, then the hive. |
| Launches on one hive end with `hive launch failed: expired` | The hive picked up the launch order more than 300 seconds after the controller sent it. | Check that the hive beats (`agentihooks hive list` shows `live`) and its `agentihooks-hive` service runs. |
| A remote task's Contract and proof fold is empty | The hive stopped beating for more than 45 seconds, so its synced copy expired, or the agent has not written a line yet. | Check `agentihooks hive list`. The lines come back on the hive's next heartbeat. |
| A relaunched agent on the same hive does not see proof lines written on another machine | `progress.md` and `proof.md` already on a hive are kept, never merged. | Copy the missing lines into the hive's work folder, or append them from the agent. |

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

On the move, the tick writes a recovery handoff that names its unknowns, bumps the master seat generation, fences
the old execution and starts the successor on the hive master placement picks, or unpinned when none qualifies.

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
3. Sets itself `ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=<workers.maxAgents>`.
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
