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

Set them with `agentihooks hive set <id> key=value ...`, or on the ledger page's
[Hives panel](#ledger-hives-panel), which writes through `PATCH /api/v1/hives/<id>` and needs the operator.

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

## Ledger Hives panel

The Swarm tab of the ledger page shows a Hives panel after Quota. It reads the hive settings API, edits hive settings
in place and moves the master to another hive.

### Panel

| Column | Shows |
|---|---|
| hive | The hive's name; hovering shows its id. |
| state | `live` while the last heartbeat is under ninety seconds old, else `quiet`. |
| ui, ephemeral | `yes` or `no`, editable. |
| roles | Comma list, editable. |
| prefer | Comma list of `role:rank`, editable. |
| max agents | Whole number, editable. |
| agents | Working agents against max agents, such as `1/3`. |
| slots | One line per slot: harness, account and the sessions the hive reports on it. |
| master | The live master running on the hive, from any swarm, or `—`. |
| (no header) | The move master button, which moves this ledger's master to the hive. |

The agents figure is the placement load: worker sessions the hive reports, without its interactive master
sessions, plus this runtime's launching, working or suspect non-master agents on the hive that the hive has not
reported yet. Placement compares the same figure with max agents.

- The header count reads `<live> live · <n> hives`, or `—` when the read failed.
- The Quota header count ends with each hive's `<name> <agents>/<max agents>`, such as
  `2 accounts · probed 40s ago · laptop 1/3 · pod-a 0/4`.
- The panel reads `GET /api/v1/hives` at most every fifteen seconds while the Swarm tab is open. An empty table reads
  `Reading hives…` before the first answer, `No hives registered.` when the registry is empty, and
  `Could not read hives.` when the read failed.
- Swarm tab redraws skip the table while one of its fields holds focus; each fresh read still redraws it.
- The panel folds and keeps its folded state, its height and its column widths across reloads, like the other Swarm
  tab panels.

### Editing a hive

Change a field and leave it, or pick a new `yes` or `no`: the page sends that one field as
`PATCH /api/v1/hives/<id>` with the ledger's credential and its slug. The note line shows the write pending, then
done, or the server's error message, such as `role qa needs a UI, and this hive has ui=no`. The row then shows the
hive as the server stored it.

The page converts what was typed before it sends it:

| Field | Typed | Sent |
|---|---|---|
| roles | `eng, ci` | `["eng", "ci"]` |
| prefer | `master:1,qa:2` | `{"master": 1, "qa": 2}` |
| max agents | `4` | `4`; anything other than digits is sent as text and refused |

Only the operator can edit. An agent's credential gets
`403 Hive settings need the operator`.

### Move master

The button sends the swarm control `master_move` with the hive id. The server runs
`agentihooks swarm <slug> master move --to <hive>`, so the move behaves as described in
[Headless fallback](#headless-fallback) and in the [swarm commands](swarm.md): the live master hands off and its
successor starts on that hive.

The button is off when:

- the Swarm tab has no swarm status yet, or another swarm control is still pending;
- the hive is quiet;
- this ledger's master already runs on that hive.

The command can still refuse a hive the button allows, such as one placement would not give the master; the
control then answers `502 request_refused` and the note line shows the server's answer.

```bash
curl -X PUT "http://127.0.0.1:8765/api/v1/ledgers/$SLUG/swarm/controls" \
  -H "Content-Type: application/json" -H "X-Ledger-Token: $LEDGER_TOKEN" \
  -d '{"action": "master_move", "hive": "workstation"}'
```

A hive id must start with a letter or digit and hold only letters, digits, dots, dashes or underscores, at most 63
characters; any other answers `400 request_refused` with `A master move needs a hive id`. A control from an agent's
credential answers `403 forbidden` with `swarm controls need the operator`.

### Hive settings API

| Method and path | Who | Answer |
|---|---|---|
| `GET /api/v1/hives` | no ledger credential needed | Every registered hive. |
| `GET /api/v1/hives/<id>` | no ledger credential needed | One hive. |
| `PATCH /api/v1/hives/<id>` | the operator | The hive after the change. |

Every answer is `{"data": ..., "revision": "<sha256 of data>"}`. A hive carries its stored record (`id`, `name`,
`ui`, `ephemeral`, `roles`, `prefer`, `max_agents`, `harnesses`, `slots`, `interactive`, `repos`,
`heartbeat_at`, `version`) and three computed fields:

- `live`: `true` while the heartbeat is under ninety seconds old.
- `agents`: the placement load described under [Panel](#panel).
- `master`: `{"slug", "name"}` of the live master on the hive, or `null`.

Each slot also gains `agents`, the sessions the hive reports for that harness and account.

```bash
curl -s http://127.0.0.1:8765/api/v1/hives/pod-a
```

```json
{
  "data": {
    "id": "pod-a", "name": "pod-a", "ui": "no", "ephemeral": "yes",
    "roles": ["eng", "ci"], "prefer": {}, "max_agents": 4, "agents": 1,
    "live": true, "master": null,
    "slots": [{"harness": "claude", "account": "work", "kind": "subscription", "cap": 2, "provider": "", "agents": 1}],
    "...": "..."
  },
  "revision": "9f2c..."
}
```

The PATCH body takes one or more of these fields and nothing else:

| Field | Type |
|---|---|
| `name` | string, 1 to 200 characters |
| `ui`, `ephemeral` | `"yes"` or `"no"` |
| `roles` | array of `master`, `qa`, `frontend`, `eng`, `ci`, `plan`, `dispatch` |
| `prefer` | object of role to whole number rank |
| `max_agents` | integer, 1 or more |

The server checks the body against that schema, then applies it with the same rules as `agentihooks hive set`, so a
UI role on a `ui=no` hive, a `prefer` role the hive does not take, or a rank or max agents below 1 is refused. The
credential is the ledger token in `X-Ledger-Token` and the ledger it belongs to in `X-Ledger-Slug`.

```bash
curl -X PATCH http://127.0.0.1:8765/api/v1/hives/pod-a \
  -H "Content-Type: application/json" \
  -H "X-Ledger-Slug: $SLUG" -H "X-Ledger-Token: $LEDGER_TOKEN" \
  -d '{"roles": ["eng", "ci", "plan"], "max_agents": 4}'
```

| Status | Code | When |
|---|---|---|
| 400 | `schema_invalid` | The body has no field, an unknown field or a wrong type (`Request does not match the resource schema`), or the hive registry refuses the value and names why. |
| 403 | `forbidden` | `Missing or wrong ledger credential`: no `X-Ledger-Slug`, an unknown ledger or a wrong token. `Hive settings need the operator`: the credential is an agent's. |
| 404 | `resource_missing` | `No such hive`. A PATCH never creates a hive; register one with `agentihooks hive join`. |
| 405 | `method_not_allowed` | Anything but GET on the list, or GET and PATCH on one hive. |
| 503 | `storage_error` | Redis is unreachable or a hive record is unreadable. |

### Limitations

- Masters do not count in the agents figure, matching max agents.
- Settings the hive reports itself (`harnesses`, `slots`, `interactive`, `repos`, `heartbeat_at`, `version`)
  cannot be changed through the API.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not read hives.` | The server answered an error, usually 503 with Redis down. | `curl` the list to read the error, then restore Redis. |
| An edit shows `Hive settings need the operator` | The page was opened with an agent's credential. | Open the ledger with the operator's credential. |
| An edit shows `prefer names qa, a role this hive does not take` | `prefer` named a role missing from `roles`. | Add the role first, then the rank. |
| move master stays off | The hive is quiet, the master is already there, or a control is pending. | Wait for the hive's heartbeat or the pending control. |
| A hive stays `quiet` | `hive run` is not publishing. | `systemctl --user status agentihooks-hive` on that machine. |

## Related pages

- [Deployment modes](../reference/deployment-modes.md): where the controller, ledger and Redis run, and the
  regulated-site recipe.
- [Swarm](swarm.md): the master, seats, the minute tick and the swarm commands.
- [Claude account load balancing](load-balancing.md): subscription and api slots.
- [Hosted ledger auth](../reference/hosted-ledger-auth.md): hive tokens and edge mode.
