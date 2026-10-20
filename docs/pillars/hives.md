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

## Repos and checkouts

Each heartbeat reports the repos the hive can work in. `hive run` scans one folder, `HIVE_REPO_ROOT`, default
`~/dev`: every direct child that is a git checkout, and every checkout one level below a child that is not one.
Worktrees count; their origin is read from the shared git directory.

For every checkout the hive reports two names, so a task matches whichever form its swarm uses:

- the absolute path, such as `/home/me/dev/agentihooks`;
- the origin identity, the `origin` remote as lowercase `host/owner/name`, such as
  `github.com/the-cloud-clockwork/agentihooks`. A checkout with no `origin` remote reports only its path.

```bash
# Workers keep their clones under /srv/repos instead of ~/dev
HIVE_REPO_ROOT=/srv/repos agentihooks hive run

# The repos the swarm sees for one hive
agentihooks hive show lab-vm        # the "repos" list
```

Placement drops a hive whose list lacks the task's repo, with "no hive for `<role>` lists the repo `<repo>`"
("no headless hive for ..." when only headless hives were tried).
When a launch names the repo by origin identity, the hive starts the agent in its own checkout with that origin,
the first by path when it holds several. A repo given as a path launches in that path.

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

A hive names a subscription slot by its `AH_CC_TOKEN_<slug>` slug; a hosted controller names the same account by
its Secret key. The two may differ only in `_` against `-`, and master placement treats them as one account. A
master on a hive is held to the session cap that hive reports for the account, not the hosted controller's cloud
worker cap. The hive computes that cap from its own quota reading of the account, with the bands in
[Claude account load balancing](load-balancing.md), and reports 0 while it has no fresh reading. When the
reservations on the account already reach the cap, the launch is not started and reports "account reservation is
full".

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

## Launches on a hive

The controller hands a hive a launch order; the hive's own Redis user receives it, launches the agent and writes
the acknowledgement the controller reads.

### What a launch carries

The order points at a payload that carries everything the agent needs from the controller's machine:

| Field | Content |
|---|---|
| Opening prompt | The full prompt the agent starts with, built on the controller. |
| Digest | The sha256 of that prompt. |
| Work folder seeds | `steering.md` whole, and the last 65536 characters of `progress.md` and `proof.md`, when the task has a work folder. |

The hive refuses a payload with no opening prompt, or whose digest does not match it, and launches with the carried
prompt unchanged. Because the hive now requires the prompt, upgrade controllers before hives.

### Work folders on a hive

The hive writes the seeds to `~/.agentihooks/swarm/<swarm>/tasks/<task>` under its own home, after it checks the
controller epoch. `steering.md` always takes the controller's copy. `progress.md` and `proof.md` are written only
when the hive holds no copy yet, so a task relaunched on the same hive keeps the lines its earlier agent wrote.
Resume and recovery prompts name that folder as `~/.agentihooks/swarm/<swarm>/tasks/<task>`, the path on the
hive.

Every heartbeat the hive publishes the last three lines of `progress.md` and `proof.md` for the latest execution
of each task it runs or last ran, masters excluded, under its own hive key. The copy expires 45 seconds after the
last beat. The ledger page's work folder tails read the copy of the hive that runs, or last ran, the task's latest
execution, and the controller's own folder for tasks with no hive.

### Refusal causes

A launch the hive cannot complete is acknowledged `failed` with a cause, and the controller records the launch as
failed with "hive launch failed: `<cause>`".

| Cause | Meaning |
|---|---|
| `identity` | The order does not match the admitted execution: generation, lane, task, harness, profile, account slot, route, target hive or state differ, the seat holds a newer execution, or the started agent reported a different harness, profile or account. |
| `prompt` | The payload is missing, is not the order's task, or carries no opening prompt or a digest that does not match it. |
| `refused` | The controller epoch moved on before or right after the launch, or the launch raised a swarm error. |
| `expired` | The agent started after the order's expiry; the hive stops it. |
| `ambiguous` | The agent may have started but its process could not be identified; the slot reservation decides it. |
| `<status>: <detail>` | The local runtime refused the spawn, such as `refused: <why>` or `unavailable: <why>`, with the runtime's own reason. |

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

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| A launch waits with "no hive for `<role>` lists the repo `<repo>`" | `agentihooks hive show <id>`, the `repos` list | Clone the repo under `HIVE_REPO_ROOT` with an `origin` remote matching the swarm's repo name, or point `HIVE_REPO_ROOT` at the clones. |
| A launch waits with "no live hive takes" a role | `agentihooks hive list`: `stale` hives, roles | Restart `agentihooks hive run` (`agentihooks hive install` keeps it running), or add the role with `agentihooks hive set`. |
| Every launch on a hive fails with "hive launch failed: prompt" | The controller and hive versions | Upgrade the controller first; an older controller sends no opening prompt. |
| A hive's agents show no work folder lines on the ledger page | The hive's heartbeat age in `agentihooks hive list` | The copy expires 45 seconds after the last beat; restart `hive run`. |
| A master launch on a hive reports "account reservation is full" | `agentihooks hive show <id>`: the slot's `cap` for the account | A `cap` of 0 means the hive has no fresh quota reading for the account; otherwise end a session on that account or wait for its five hour window to recover. |

## Related pages

- [Deployment modes](../reference/deployment-modes.md): where the controller, ledger and Redis run, and the
  regulated-site recipe.
- [Swarm](swarm.md): the master, seats, the minute tick and the swarm commands.
- [Claude account load balancing](load-balancing.md): subscription and api slots.
- [Hosted ledger auth](../reference/hosted-ledger-auth.md): hive tokens and edge mode.
