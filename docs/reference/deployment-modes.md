---
title: Deployment modes
parent: Reference
nav_order: 26
---

# Deployment modes
{: .no_toc }

`AGENTIHOOKS_DEPLOYMENT` decides where the controller launches agents. Every mode keeps one ledger database and
one Redis, which every ledger server and controller of the swarm share.

1. TOC
{:toc}

---

## Modes

| Mode | Set up with | Controller | Ledger and Redis | Where agents launch |
|---|---|---|---|---|
| `local` (default) | `agentihooks controller run` on the workstation | On the workstation | Local ledger server; Redis from `AGENTIHOOKS_SWARM_REDIS_URL`, default `redis://127.0.0.1:6379/0` | On the workstation, in herdr panes, inside its host capacity. |
| `compose` | `compose.yaml`: services `swarm`, `controller`, `redis`, and `hive` under `--profile join` | The `controller` service | The `swarm` service on `SWARM_BIND:SWARM_PORT` (default `127.0.0.1:8765`); the stack Redis with per hive ACL users | On joined [hives](../pillars/hives.md): engineer and CI work, master work on UI hives only, and any task pinned to a hive. Other lanes are refused with "the workstation hive spawns" that lane's work. With no hive placement the controller reconciles without spawning. |
| `distributed` | The `agentihooks-swarm` Helm chart (`deployment: distributed`) | The chart's controller | One ledger replica and the chart Redis, or the hosted ledger through `SWARM_URL` | On hives as in `compose`, including worker hive pods; without hive placement, only through the Kubernetes runtime. |

- With `distributed` set on a workstation the hub runs the swarm engine: `agentihooks swarm tick` ticks no swarm, and
  the swarm user services hold only the `agentihooks-spoke` service, which takes master work from the bus. The tick,
  wait, waker, prober and tick watch units are retired and no scheduled tick is registered.
- In any mode other than `local`, `AGENTIHOOKS_HIVE_REDIS_URL` wins over the swarm Redis URL, and the workstation
  host capacity checks are off.
- A client with `SWARM_URL` set, with `LEDGER_URL` set and no mode, or in a mode other than `local`, uses the
  remote ledger and never starts a local ledger server. See [Client mode resolution](#client-mode-resolution). The
  hosted service's auth is in [Hosted ledger auth](hosted-ledger-auth.md).
- Whether a hive runs on subscriptions or api keys is a property of its slots, chosen per hive. See
  [Claude account load balancing](../pillars/load-balancing.md).

Adding machines, roles, placement, failover timing and fencing are on the [Hives](../pillars/hives.md) page.

## The hub and the bus

The event driven design has three parts:

- a hub on Anton that runs the swarm's reactors;
- a NATS server with JetStream;
- hives that pull offered work (see [Pulling work from the hub](../pillars/hives.md#pulling-work-from-the-hub)).

Each swarm picks its engine with its `engine` setting, `legacy` or `events`; see
[Event driven engine](../pillars/swarm.md#event-driven-engine).

| Mode | Engine | Bus | Hub |
|---|---|---|---|
| `local` | `legacy`; `events` only with a hub on the same bus | `memory` unless the bus settings name NATS | None runs by default. The hub's `readiness` reactor offers nothing on the `memory` bus, so an `events` swarm needs a hub on a NATS bus. |
| `compose` | `legacy`; `events` only with a hub on the same bus | The stack's `nats` service: `AGENTIHOOKS_BUS=nats` at `nats://nats:4222`, changed only through the stack settings `SWARM_BUS` and `SWARM_BUS_URL`; the `controller` and `hive` services take their mode only from `SWARM_DEPLOYMENT`, and the base stack services their scheduler state backend only from `SWARM_SCHEDULER_STATE_BACKEND` | No hub service. The Compose task smoke runs `agentihooks hub run --once` in the `swarm` service to settle a task end to end. |
| `distributed` | `events` through the hub; a `legacy` swarm is ticked by the chart's controller, never by the workstation | NATS with JetStream from the `agentihooks-swarm` chart (`nats.enabled`, JetStream on) | The chart's `<release>-hub` Deployment, with `hub.enabled: true` and 2 replicas by default. It runs `agentihooks hub run`, scheduled only on nodes without the `anton.io/capacity-type` label. Each pod turns ready only once it has subscribed to slot requests (see [Hub readiness and rollouts](#hub-readiness-and-rollouts)). |

### Hub reactors

`agentihooks hub run` creates the hive buckets, then runs every reactor. Each reactor has a durable consumer named
`hub-<name>`. Singleton reactors hold a lease in `hub-leases`, so with two replicas only one replica runs each of
them. Every reactor takes up to 50 deliveries of an event, waiting 1, 5, 15, 60 and then 300 seconds between attempts.

With a remote ledger, `agentihooks hub run` first issues the replica its own service credential, `hub-<hostname>.<token>`, and exports it as `AGENTIHOOKS_CONTROLLER_CREDENTIAL`. The readiness, admission, lifecycle and inbox wake reactors run their ledger writes in process over the shared database, carrying no credential for that call; this credential is what the ledger's own authority check (`dispatch`, `ledger_authority.controller`) accepts for any call these reactors still make as real HTTP, such as a proof ledger's own routes. The ledger accepts it while the digest key `agentihooks-hive:controller:hub-<hostname>` lives. The key expires after five minutes, and a renewal thread renews it every hundred seconds, so a replaced pod's credential lapses on its own. Issuing it never retires the controller's credential or the other replica's. The hub's Redis user needs GET and SET on `~agentihooks-hive:controller:*` beside `~agentihooks-hive:controller`, and every ledger server's Redis user needs GET on it.

| Reactor | Reacts to | Does |
|---|---|---|
| `timers` | `swarm.*.deadline.*` | Keeps scheduled deadlines and publishes `deadline.expired` when one is due. |
| `inbox-wake` | `swarm.*.inbox-wake.requested` and a poll | Schedules the wake deadline for an inbox item and relays queued wake items onto the seat inbox. |
| `inbox-wake-expiry` | `swarm.*.deadline.expired` | When the deadline expires, redelivers or wakes the item and schedules its next deadline. Sends no wake while an outage the tick published is open (see [Outage holds for agents the hub started](../pillars/hives.md#outage-holds-for-agents-the-hub-started)). |
| `hive-loss` | `swarm.*.execution.started` | Publishes `execution.exited` for executions on a lost hive. |
| `claim-lives` | `swarm.*.execution.started` | Counts each successful spoke launch once toward the task's three started lives cap. |
| `lifecycle` | `swarm.*.execution.exited` | Orders the hive of an agent whose pane died to reopen its own conversation in its seat, up to three times, before it drops the agent (see [Resuming a dead agent the hub started](../pillars/hives.md#resuming-a-dead-agent-the-hub-started)). Refunds a started life on a park, quota handoff or operator preemption, releases the slot and claim, and settles the task. An exit a hive reports after an execution stop carries reason `stopped` and the stop's `stop_reason`, such as `ended` for an agent whose work ended (see [Stopping an agent whose work ended](../pillars/hives.md#stopping-an-agent-whose-work-ended)). |
| `reconciler` | A five minute poll; it also subscribes to `swarm.*.reconcile.requested`, which nothing publishes yet | Rebuilds missing starts and ends executions that are gone. |
| `readiness` | A poll; it also subscribes to `swarm.*.readiness.requested`, which nothing publishes yet | Offers the claimable tasks of every `events` swarm to the lane work queues. |
| `preemption` | A one minute poll; it also subscribes to `swarm.*.preemption.requested`, which nothing publishes yet | Asks the lowest ranked holder of a full lane to hand off when an urgent or focused task of a running `events` swarm still waits one poll later, as the legacy tick does. |
| `pulls` | `swarm.*.task.*` and a poll | Publishes pull request, merge queue and check events, also fed by `agentihooks github-webhook`. |
| `launch-checks` | A ten second poll; it also subscribes to `swarm.*.launch-checks.requested`, which nothing publishes yet | Judges the launch check of each worker the hub admitted, from the launch time its execution started write records. A worker whose hive reports no launch fails 300 seconds after admission. A failed worker is stopped on its hive, and its task relaunches once. |

### Hub readiness and rollouts

A chart hub pod turns ready only once its replica has subscribed to slot requests. Every hive asks the hub for an account slot
on `slots.<swarm>.<hive>`, and the hub answers in queue group `hub-slots` (see
[Hub slot requests and the hive binding](swarm-bus-scope.md#hub-slot-requests-and-the-hive-binding)). A request
that reaches NATS while no hub replica subscribes fails at once on the hive with
`TimeoutError: no responders for 'slots.<swarm>.<hive>'`. Without a readiness
probe, Kubernetes counts a hub pod ready as soon as its container starts, so a rollout could end the last old hub
pod before the new one subscribes and leave the swarm with no slots responder.

#### How the hub marks itself serving

`agentihooks hub run` reads a file path from `AGENTIHOOKS_HUB_SERVING_FILE` and handles it in this order:

1. At start it deletes the file, so a file left by an earlier run never reads as serving.
2. It issues its service credential when the ledger is remote, opens the bus and creates the hive buckets.
3. It subscribes its `slots.*.*` responder (queue group `hub-slots`) and its `exits.*.*` responder (queue group
   `hub-exits`).
4. It creates the file, then starts its reactors.

The hub never deletes the file while it runs or when it stops. With the variable unset or empty, the hub creates no
file and runs the same way. A hosted hub that refuses to start for want of the shared database exits before step 2,
so its file stays absent. Readiness reports only that the hub subscribed once: the file stays while the process
runs, so a replica that later loses NATS stays ready.

#### What the chart sets

| Value | Default | Effect |
|---|---|---|
| `hub.servingFile` | `/tmp/hub.serving` | The path the chart sets as `AGENTIHOOKS_HUB_SERVING_FILE` on the hub container and the file its readiness probe tests. Required: an empty value fails the render. Its folder must exist and be writable by the hub, because the hub creates only the file. |
| `hub.replicas` | `2` | Above 1, the hub Deployment rolls with `maxSurge: 0` and `maxUnavailable: 1`. At 1 the chart sets no strategy and the Kubernetes default applies, which starts the new pod beside the old one and ends the old one once the new one is ready. |

The chart renders the hub Deployment, and with it the variable and the probe, only when `hub.enabled` is true and
`hiveOnly` is false. The rendered hub container carries the variable and the probe on the same path:

```yaml
env:
  - name: AGENTIHOOKS_HUB_SERVING_FILE
    value: "/tmp/hub.serving"
readinessProbe:
  exec:
    command: [test, -f, "/tmp/hub.serving"]
  periodSeconds: 2
```

The probe sets only its period, so the Kubernetes defaults hold for the rest: a 1 second timeout, one passing test
to turn ready and three failed tests to turn unready. The hub has no liveness or startup probe, so a pod that never
creates the file stays unready and is never restarted for it.

With two replicas, Kubernetes ends one old pod, starts its replacement, and ends the second old pod only once the
replacement is ready. Ready means the replacement has subscribed its slots responder, so at least one replica
answers slot requests through the whole rollout. A replacement that never turns ready holds the rollout: the second
old pod keeps serving, and after the default progress deadline of ten minutes `kubectl rollout status` reports that
the Deployment exceeded it.

#### Setting another path

The chart writes `AGENTIHOOKS_HUB_SERVING_FILE` after every `hub.env` and `hub.secretEnv` entry. When one container
lists a variable twice, Kubernetes uses the last value, so a `hub.env` entry of that name cannot move the hub off the
file its probe tests. Change the path through `hub.servingFile`, which moves both:

```yaml
hub:
  enabled: true
  servingFile: /tmp/agentihooks-hub.serving
```

An empty or null `hub.servingFile` stops the render, so neither `helm template` nor `helm upgrade` produces a hub
whose probe could never pass:

```bash
helm template agentihooks-swarm deploy/helm/agentihooks-swarm \
  --set hub.enabled=true --set hub.bus.url=nats://nats.swarm:4222 --set hub.servingFile=
```

Helm exits with an error carrying the chart's message:

```text
hub.servingFile is required: the hub creates that file once it answers slots requests, and its readiness probe waits for it
```

#### Hub readiness and image tags

The hub change that creates the file merged into dev before the chart change that adds the probe, so every release
whose chart has the probe ships a hub image that creates the file. A chart with the probe, run with `image.tag`
pinned to an older image, gives hub pods that never turn ready. Run the hub on the image of its chart's release.

#### Checking hub readiness

The examples use the release name `agentihooks-swarm` in namespace `agentihooks`:

```bash
kubectl -n agentihooks rollout status deployment/agentihooks-swarm-hub
kubectl -n agentihooks get pods -l app.kubernetes.io/instance=agentihooks-swarm,app.kubernetes.io/component=hub
kubectl -n agentihooks get deployment agentihooks-swarm-hub \
  -o jsonpath='{.spec.template.spec.containers[0].readinessProbe.exec.command}'
kubectl -n agentihooks exec <hub pod> -- test -f /tmp/hub.serving && echo serving
```

The rollout reports `successfully rolled out` once every hub pod serves. Each serving pod shows `1/1` in the `READY`
column, the probe command names `test -f /tmp/hub.serving`, and the last command prints `serving`.

#### Hub readiness troubleshooting

| Symptom | Cause and fix |
|---|---|
| A new hub pod stays `Running` at `0/1` ready and the rollout does not finish | The hub has not subscribed its slot responders. Read `kubectl -n agentihooks logs <hub pod>`: a bus connection error points at `hub.bus.url` or the NATS server, as in [Bus settings](#bus-settings). The old pod keeps serving meanwhile. |
| A new hub pod restarts in `CrashLoopBackOff` | The hub exited before it served. A hosted hub without the shared database logs `hub: a hosted hub writes task claims in process over the shared database and needs AGENTIHOOKS_SWARM_DATABASE_URL`; give the hub that setting. A missing folder for `hub.servingFile` logs a traceback ending in `FileNotFoundError` on that path; point `hub.servingFile` at a folder that exists. |
| `helm upgrade` fails with `hub.servingFile is required` | The values set `hub.servingFile` empty or null. Remove that override or set a path. |
| Every new hub pod stays `0/1` and `test -f` on the serving file keeps failing | `image.tag` pins an image older than the chart, whose hub never creates the file. Compare `kubectl -n agentihooks get deployment agentihooks-swarm-hub -o jsonpath='{.spec.template.spec.containers[0].image}'` with the chart version and run the image of the chart's release, as in [Hub readiness and image tags](#hub-readiness-and-image-tags). |
| Hives time out on slot requests while every hub pod is ready | Look in the hub log for requests dropped after waiting in the serve queue; see [How the hub serves requests](swarm-bus-scope.md#how-the-hub-serves-requests) for the log line and the limits. |

#### Hub readiness regression guards

| Guard | What it holds |
|---|---|
| `tests/hub/test_runner.py::test_the_hub_marks_itself_serving_only_after_its_slots_and_exits_responders_subscribe`, feature `hub-serving-readiness` | The hub deletes a stale file at start and creates the file only after its slots and exits responders subscribe. |
| `tests/test_swarm_chart_hub_readiness.py::test_the_hub_turns_ready_only_once_its_serving_file_exists`, feature `hub-ready-only-when-serving` | The hub template sets the variable after the `hub.env` and `hub.secretEnv` entries, the probe tests `hub.servingFile`, and the default sits under `/tmp`. |
| `tests/chart_hub.py`, run by `deploy/helm/agentihooks-swarm/ci/kind-smoke.sh` in the Helm chart in kind workflow | The rendered hub probe tests the same path as its variable, an empty `hub.servingFile` fails the render, and `hub.env.AGENTIHOOKS_HUB_SERVING_FILE=/elsewhere` leaves the variable and the probe on one file. |

### Bus settings

| Setting | Meaning |
|---|---|
| `AGENTIHOOKS_BUS` | `memory` (default) or `nats`. |
| `AGENTIHOOKS_BUS_URL` | The NATS URL; default `nats://127.0.0.1:4222`. |
| `AGENTIHOOKS_BUS_CREDENTIALS` | A NATS credentials file, or `openbao:<path>`, which reads that record's `credentials` field. |
| `AGENTIHOOKS_BUS_BIND` | `1` binds to streams and buckets the hub created, without creating any itself. |
| `AGENTIHOOKS_BUS_REPLICAS` | Replica count of every stream and bucket a provisioning bus creates; default `1`. At start it raises each stream holding fewer replicas in place and keeps a higher count. Set it to the server count of a NATS cluster; a single server refuses more than one. |

A hive gets these settings from its invite. The hub operator runs `agentihooks hive invite <label> --bus-url <url>`.
On the hive, `agentihooks hive join <invite>` writes `AGENTIHOOKS_BUS=nats` and the URL into `hive.env`. When the
join returns NATS credentials, it saves them privately and adds `AGENTIHOOKS_BUS_CREDENTIALS` and
`AGENTIHOOKS_BUS_BIND=1`. Each member's NATS user is limited to the subjects its hive needs. The workstation root
hive has no invite; it gets the same settings from `agentihooks hive root-bus`, described next.

### Root hive bus settings

The workstation is the root hive, hive id `root`. It never joins through an invite, so no `hive.env` carries its
bus settings, and a join cannot supply them: joining makes the host a hive member whose id replaces `root`, so the
offers the tick pins to the root hive would find no taker. Without bus settings `hive run` stays on the memory bus,
starts no lane spoke and logs:

```text
hive lane spokes refused: the memory bus never sees the hub's ready offers; a hive needs AGENTIHOOKS_BUS=nats and AGENTIHOOKS_BUS_URL
```

`agentihooks hive root-bus` mints the root hive's NATS member credential with the hub signer, writes the bus
settings that only the hive user service loads, proves the hub accepts the credential and restarts the service.
No credential value is printed; the settings name the credential file by its path.

#### Settings it reads

The command loads the operator env files first, so these can live in `~/.agentihooks/.env`:

| Setting | Use |
|---|---|
| `AGENTIHOOKS_NATS_SIGNER` | Required. The hub signer record: an OpenBao path under `agentihooks/hub/`, or `file:<folder>` for a signer folder. The record's `url` field becomes the bus URL. |
| `AGENTIHOOKS_NATS_TRUSTED_OPERATOR` | Required. The operator public key the NATS server trusts. |
| `OPENBAO_URL`, `OPENBAO_TOKEN` | Required for an OpenBao signer. They also let the command add `root` to the record's `member_swarms` listing. |
| `AGENTIHOOKS_NATS_SIGNER_SYNC` | Optional, `<namespace>/<name>` of the ExternalSecret that copies the signer record into the cluster; default `agentihooks-swarm/agentihooks-swarm-nats-signer`. An empty value skips the sync. Only an OpenBao signer is synced. |

The host must also meet three conditions:

- It is not a joined hive member: `AGENTIHOOKS_HIVE_ID` is unset, or the Redis URL in use differs from
  `AGENTIHOOKS_HIVE_REDIS_URL`.
- Its hive id is `root`. The id is `SWARM_HIVE_ID`, then the seeded hive id, then the host name.
- Its agentihooks home is `~/.agentihooks`. The hive user unit reads env files only from there, so a run with
  `AGENTIHOOKS_HOME` set to another folder is refused.

The signer record needs a `member_swarms` field; `agentihooks hive scope` on the hub adds it. See
[Swarm scoped bus credentials](swarm-bus-scope.md#when-a-credential-is-scoped).

#### Run it

```bash
# List root for every swarm in the swarm store
agentihooks hive root-bus

# List root only for the named swarms; repeat --swarm for each one
agentihooks hive root-bus --swarm rig-grade-swarm --swarm demo
```

The command runs these steps in order and stops at the first refusal:

1. Loads the operator env files and checks the three host conditions above.
2. Reads the hub signer. The wanted swarms are the `--swarm` names, or every swarm in the swarm store when none is
   named.
3. When the record's `member_swarms` entry for `root` already holds every wanted swarm, leaves the record as it is.
   Otherwise it appends the missing swarms to that entry, creating the entry if needed and keeping every other
   member, then asks the cluster to resync the ExternalSecret with
   `kubectl annotate externalsecret <name> --namespace <namespace> force-sync=<time> --overwrite` and reads the
   signer again.
4. Mints the root member credential, a user JWT named `hive-root` signed by the swarm account. It grants only the
   subjects of the swarms the record lists for `root`; see
   [What a scoped member is granted](swarm-bus-scope.md#what-a-scoped-member-is-granted). The credential
   may take at most 61,440 bytes, the 65,536 byte NATS control line less 4,096 bytes of headroom; a larger one is
   refused before any file is written.
5. Writes the credential to `~/.agentihooks/root-bus/bus.creds` and the settings to
   `~/.agentihooks/root-bus/bus.env`, both mode 0600, in a folder created with mode 0700. A rerun overwrites both.
6. Connects to the hub's NATS once with the written settings, then closes the connection.
7. Rewrites the hive user unit when its text differs, runs `systemctl --user enable --now agentihooks-hive.service`,
   then restarts the service so it loads the new settings.

A run that changed the listing prints:

```text
listed hive root for demo in the hub signer record
requested a sync of the signer record agentihooks-swarm/agentihooks-swarm-nats-signer
wrote the NATS bus settings of hive root to /home/<user>/.agentihooks/root-bus/bus.env and restarted the hive service
```

When `kubectl` fails, the second line reads
`could not force the sync of <target> (<reason>), so hive join sees it at the next refresh`, and the command goes
on. A run where `root` already held every wanted swarm prints only the last line.

#### What it writes

`bus.env` holds the bus URL from the signer record and the swarms the record lists for `root`, which can be more
than the `--swarm` names of this run:

```bash
AGENTIHOOKS_BUS=nats
AGENTIHOOKS_BUS_URL=nats://nats.example.com:4222
AGENTIHOOKS_BUS_CREDENTIALS=/home/<user>/.agentihooks/root-bus/bus.creds
AGENTIHOOKS_BUS_BIND=1
AGENTIHOOKS_BUS_SCOPED=1
AGENTIHOOKS_BUS_SWARMS=rig-grade-swarm,demo
```

The `root-bus` folder sits below the agentihooks home, so the operator env loading, which reads
`~/.agentihooks/.env`, the other `~/.agentihooks/*.env` files and `~/.env`, never reads it. The hooks, swarm
commands and other agentihooks processes on the workstation keep their own bus settings. Only the hive user unit
loads it, after the other two env files, so its lines override the same names set there:

```ini
EnvironmentFile=-%h/.agentihooks/.env
EnvironmentFile=-%h/.agentihooks/hive.env
EnvironmentFile=-%h/.agentihooks/root-bus/bus.env
```

The `-` prefix makes each file optional, so a hive without root bus settings still starts.

#### Check it

After the restart the hive runs a lane spoke for each `events` swarm and each lane not named in `HIVE_EXCLUDED`,
and each spoke logs its start in the hive journal:

```bash
journalctl --user -u agentihooks-hive --since "5 minutes ago" | grep 'hive spoke for swarm'
# hive spoke for swarm rig-grade-swarm lane eng takes swarm.rig-grade-swarm.ready.eng
```

The spokes also beat the root hive's presence entry, key `root` in each `events` swarm's `hive-presence` bucket.
Each spoke renews it when its poll starts and again every 15 seconds while the poll runs. When no spoke takes work in a pass, the
hive waits 5 seconds before the next pass. The entry holds `heartbeat_ms` and `expires_ms` and lapses 45 seconds after the last beat. With the
root credential its state subject can be read directly; the state stream name follows
[Subjects and streams per swarm](swarm-bus-scope.md#subjects-and-streams-per-swarm):

```bash
nats --server nats://nats.example.com:4222 --creds ~/.agentihooks/root-bus/bus.creds \
  stream get <state stream of hive-presence> --last-for swarm.rig-grade-swarm.state.hive-presence.root
```

#### Agents on the root hive

The root spokes launch agents through the launcher script `agentihooks init-agent` writes. For a swarm agent that
script exports `AGENTIHOOKS_BUS`, `AGENTIHOOKS_BUS_URL`, `AGENTIHOOKS_BUS_BIND`, `AGENTIHOOKS_BUS_SCOPED` and
`AGENTIHOOKS_BUS_SWARMS` from the hive's environment. It exports `AGENTIHOOKS_BUS_CREDENTIALS` only while it is a
reference, a file path or `openbao:<path>`. Inline credential text, which starts with
`-----BEGIN NATS USER JWT-----`, never reaches the launcher script. Every agent on the root hive therefore uses
the same bus and the same root credential file as its spokes.

#### Re-mint at hive start

Each time `hive run` starts, before its spokes read their settings, it checks a stored root credential:

1. When `root-bus/bus.creds` is missing under the agentihooks home it does nothing, so a hive that never ran
   `hive root-bus` reads no signer.
2. Otherwise it reads the signer and compares the stored credential's publish and subscribe grant with a fresh mint
   for `root` from the record's current listing. A stored credential that does not decode counts as different.
3. When they differ it writes both files again, loads the new settings into its own environment and logs
   `hive re-minted the root bus credential in <path>: its grant differed from a fresh mint`.

A changed `member_swarms` entry for `root` or a release that changes the member grants makes them differ. The
re-mint runs no connect check and never extends the listing. When it cannot mint, for example with
`AGENTIHOOKS_NATS_SIGNER names no hub NATS signer`, the hive logs
`hive kept the stored root bus credential (<reason>)` and runs on the stored credential.

#### Revoke

`agentihooks hive revoke root` runs three steps in order:

1. When `AGENTIHOOKS_NATS_SIGNER` names an OpenBao record under `agentihooks/hub/`, it removes `root` from the
   record's `member_swarms` field and requests the ExternalSecret sync, as `hive root-bus` does.
2. With a signer, it records the root NATS user as revoked and pushes the updated account to the NATS server.
3. It deletes `bus.env` and `bus.creds`.

```text
unlisted root from the hub signer record agentihooks/hub/nats
requested a sync of the signer record agentihooks-swarm/agentihooks-swarm-nats-signer
revoked the NATS credential of root
revoked root
```

The first line reads `the hub signer record <path> lists no member swarms for root` when the record had no entry
for `root`. A signer that is not an OpenBao hub record prints no unlist line.

A refusal prints `hive revoke refused: <message>` and exits 1. `no OpenBao client to unlist root from <path>`
means `OPENBAO_URL` or `OPENBAO_TOKEN` is unset, and nothing has changed yet. Step 2 records the revocation in
Redis before it pushes the account, so a failed push leaves `root` unlisted, the revocation recorded and both files
in place. The push fails with `hive revoke refused: NATS refused the account update (<description>)`, or with a
`TimeoutError` traceback when NATS does not answer within 10 seconds. Run `agentihooks hive revoke root` again once
NATS answers.

The command does not restart the hive service. After the next restart the hive reads its bus settings from its
other env files only; when they set none, it runs on the memory bus and its lane spokes refuse, as above. Run
`agentihooks hive root-bus` again to give the root hive a new credential; its connect check proves the hub
accepts it.

#### Limits

- `--swarm` only adds swarms. The credential covers every swarm the record lists for `root`, including those an
  earlier run listed. To narrow it, run `agentihooks hive revoke root`, then `agentihooks hive root-bus` with
  `--swarm` for each swarm the workstation serves.
- A swarm created after the last run is outside the credential, and the re-mint at hive start does not add it. Run
  `agentihooks hive root-bus` again.
- A `file:` signer folder is never updated. Its `member_swarms` file must already list `root` for every wanted swarm.
- The hive service is a systemd user unit, written to `~/.config/systemd/user/agentihooks-hive.service`. The
  command needs Linux with a systemd user session; without one it writes the files and then refuses at the restart.

#### Refusals

Each refusal prints `hive root-bus refused: <message>` and exits 1:

| Message | Cause | Fix |
|---|---|---|
| `this host joined as hive member <id>, whose hive env carries its bus settings` | The host talks Redis as a joined member | A member gets its bus settings from `hive join` and `agentihooks hive refresh` |
| `hive root-bus serves the root hive only, and this host is hive <id>` | The host's hive id is not `root` | Run it on the workstation root hive; other hives get their settings from their invite |
| `the hive user service loads its env from ~/.agentihooks, not <path>` | `AGENTIHOOKS_HOME` names another folder | Unset `AGENTIHOOKS_HOME` for the command |
| `AGENTIHOOKS_NATS_SIGNER must name the hub NATS signer to mint the root hive credential` | No signer setting | Set `AGENTIHOOKS_NATS_SIGNER` |
| `AGENTIHOOKS_NATS_SIGNER is set, so AGENTIHOOKS_NATS_TRUSTED_OPERATOR must name the operator public key the NATS server trusts` | No trusted operator setting | Set `AGENTIHOOKS_NATS_TRUSTED_OPERATOR` |
| `AGENTIHOOKS_NATS_SIGNER names an OpenBao record, so OPENBAO_URL and OPENBAO_TOKEN must be set` | OpenBao signer without OpenBao settings | Set both |
| `the NATS signer record <path> holds the system account credential, so it must live under the OpenBao path agentihooks/hub/ that only the hub role reads` | The signer path is outside `agentihooks/hub/` | Point `AGENTIHOOKS_NATS_SIGNER` at the hub record |
| `the NATS signer record <path> cannot be used (<error>)` or `the NATS signer file <path> cannot be used (<error>)` | The record cannot be read or lacks a field | Fix the record the error names |
| `the hub signer record has no member_swarms field, so it scopes no member; it mints no unscoped bus credential` | The hub mints no unscoped credential | Run `agentihooks hive scope` on the hub, then retry |
| `no swarm to list: pass --swarm or create a swarm first` | No `--swarm` and the swarm store holds no swarm | Name a swarm or create one |
| `the hub signer record does not list root for every swarm and this host cannot update it` | A wanted swarm is missing for `root`, and the signer is a `file:` folder or OpenBao settings are missing | Set `OPENBAO_URL` and `OPENBAO_TOKEN` with an OpenBao signer, or add the swarms to the folder's `member_swarms` file |
| `member_swarms contains an invalid swarm name (<reason>)` | A `--swarm` name, or a swarm name in the record, is not one subject token | Use the swarm's slug, or fix the record |
| `member_swarms must map member ids to lists of swarm names` | The record's `member_swarms` field has the wrong shape | Fix the field; see [Swarm scoped bus credentials](swarm-bus-scope.md#when-a-credential-is-scoped) |
| An OpenBao error, shown as is | Reading or updating the record's listing failed in OpenBao | Fix the OpenBao access the error names, then rerun |
| `listed hive root in the hub signer record, but a fresh read does not list it` | The record read back after the update still lacks a wanted swarm | Run the command again; if it repeats, read the record's `member_swarms` entry for `root` |
| `the root credential is <n> bytes for <k> swarms, past the 65536 byte NATS control line; name fewer swarms with --swarm` | `root` is listed for too many swarms | Narrow the listing as in Limits |
| `the hub refused the root hive credential in <path> (<error type>: <error>)` | The connect check failed: NATS is unreachable at the record's `url`, or the server rejects the credential | Fix the address or the server's operator trust and rerun. The files stay written and the service is not restarted |
| `agentihooks executable not found` | The hive unit needs the `agentihooks` command on `PATH` | Install agentihooks for the user, then rerun. The files stay written and the service is not restarted |
| `wrote <path>, but the hive user service did not restart`, with or without `(<error>)` | `systemctl --user` failed or is missing | Read `systemctl --user status agentihooks-hive`, fix it, then `systemctl --user restart agentihooks-hive` |

## Ticks started by swarm commands

Several swarm commands write their change and then run one tick pass for the swarm themselves: `swarm start`,
`pause`, `stop` (without `--now`), `reopen` and `restore`. In a cluster install those commands also run inside agent
pods, so their tick is gated on the client mode. Only the scheduled ticks spawn and reap agents there.

| Tick | Started by | Local client mode | Any other client mode |
|---|---|---|---|
| Command tick | `swarm start`, `pause`, `stop`, `reopen`, `restore` | Takes the controller lease and the tick lock, then runs a full pass | Returns `the controller tick spawns and reaps agents` and does nothing else |
| Controller tick | `agentihooks controller run`, every `AGENTIHOOKS_CONTROLLER_TICK_SECONDS` | Full pass under the controller lease | Full pass under the controller lease |
| Host tick | `agentihooks swarm tick`, the scheduled workstation tick | Full pass under the controller lease | Full pass, except with `AGENTIHOOKS_DEPLOYMENT=distributed`, where it ticks no swarm |

The client mode is the [Client mode resolution](#client-mode-resolution) rule on the environment alone (rules 4 to
7): local with `AGENTIHOOKS_DEPLOYMENT=local`, or with no mode and no `LEDGER_URL`; any other mode, or a
`LEDGER_URL` with no mode, is not local. The controller, joined hive and worker hive pods of the
`agentihooks-swarm` chart carry the chart's `deployment` value as `AGENTIHOOKS_DEPLOYMENT`, so a command tick in any
of them stops at the gate. `SWARM_URL` and the hosted ledger rules do not change the gate.

A command tick outside local mode returns before it takes the controller lease, the tick lock or any runtime. It
spawns no agent, retires no agent, writes no lease and starts no pass of the swarm loop. The command's own write still
lands first: `pause` stores `paused`, `stop` stores `stopping`, `start` and `reopen` store `running`. The controller
tick acts on that state on its next pass.

```bash
# inside a worker pod, or any shell with a non-local deployment
AGENTIHOOKS_DEPLOYMENT=distributed agentihooks swarm my-swarm pause
# the controller tick spawns and reaps agents
# {"swarm": "my-swarm", "state": "paused"}
```

- `swarm set` runs no tick. Its JSON reports `"applies": "next tick"`, and the next controller or host tick reads the
  stored setting.
- `swarm restore` resumes the snapshot's agents through the runtime before its tick, so the gate does not cover
  those resumes; only its trailing tick stops there.
- `swarm stop --now` runs no tick either. It retires the swarm's agents through the runtime itself and stores
  `stopped`, or `stopping` with the agents it could not retire.
- On a workstation in local mode, a command tick for a swarm whose controller lease another hive holds returns `the
  swarm belongs to another hive`, as described in [Fencing](../pillars/hives.md#fencing).

Before this gate, a command tick in an agent pod was fenced only by the controller lease: with no live lease it took
the lease and spawned and reaped agents beside the controller. The regression test
`test_an_agent_pod_tick_takes_no_controller_lease_and_never_spawns_or_reaps` runs a command tick in `compose` and
`distributed` mode and asserts no lease, no pass, no spawn and no retire, then runs `controller.run_once` and asserts
the controller takes the lease and runs the pass. Red on the earlier code:
[run 39371236925](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39371236925); green:
[run 39371557233](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39371557233). Delivered in
[#5115](https://github.com/the-cloud-clockwork/agentihooks/pull/5115); [#5551](https://github.com/the-cloud-clockwork/agentihooks/pull/5551)
moved the gate onto the shared client mode rule.

| Symptom | Cause | Fix |
|---|---|---|
| A swarm command prints `the controller tick spawns and reaps agents` and no agent starts | The command ran outside local client mode | Wait for the controller's next pass, or check its log with `kubectl -n agentihooks-swarm logs deploy/agentihooks-swarm-controller` |
| A workstation command prints the same line | `LEDGER_URL` is set with no `AGENTIHOOKS_DEPLOYMENT`, or the mode is not `local` | Set `AGENTIHOOKS_DEPLOYMENT=local` when the workstation runs the swarm engine; otherwise the controller ticks the swarm |
| Agents still do not change after the controller's interval | No controller holds the swarm, or it serves another swarm | Check `controller.api.swarm` and `controller.hostSwarms` in [Configure the controller](#configure-the-controller) |

## Serving a live swarm from a distributed controller

A workstation swarm can run its engineer and CI work on a Kubernetes cluster while its master, planners and
dispatcher stay on the workstation. The swarm first moves to the hosted ledger, which hands its controller lease to
the cluster's controller; the chart's controller then serves that swarm and launches only its `eng` and `ci` lanes
as worker Pods. The Anton deployment runs `rig-grade-swarm` this way; the values and names below are Anton's.

### Move the swarm to the hosted ledger

Every ledger client, workstation or hive, reads and writes a swarm's ledger by running the ledger API route code in
process against the one shared repository its address names: Postgres when `AGENTIHOOKS_SWARM_DATABASE_URL` is set,
else the SQLite folder under `LEDGER_DIR`. A browser's reads and the live event stream still go to the ledger
server over HTTP, and a browser's writes go to the ledger API service ([The hub and the
bus](#the-hub-and-the-bus); [Shared ledger database sessions](shared-ledger-database.md)); every other reader and
writer, including a worker Pod's, already shares the one database once that setting is the same everywhere. The
API service, its chart values and the method routing are in [Ledger write path](ledger-write-path.md).

Moving a swarm onto a distributed controller is therefore only a Redis and database change, with no staged
activation step: point the chart's controller at the same `AGENTIHOOKS_SWARM_REDIS_URL` (or
`AGENTIHOOKS_HIVE_REDIS_URL`) and the same `AGENTIHOOKS_SWARM_DATABASE_URL` the workstation uses, and let it take
the swarm's controller lease the way any hive takes over a swarm it does not already hold (fencing; see
[Fencing](../pillars/hives.md#fencing)). There is no `api-url-pending` setting, no `ledger-ready` command and no
activation refusal to work through; `agentihooks swarm <id> set api-url=...` is gone, since `api_url` is written
only by the controller itself, naming its own worker-registration address ([Configure the
controller](#configure-the-controller)).

### Retire the workstation tick

A swarm whose stored `api_url` names its controller is served in Anton. While every swarm in the workstation's store
is served in Anton or stopped, the next machine wide `agentihooks swarm tick` reconcile cancels the scheduled tick
cron and removes the
`agentihooks-swarm-waits`, `agentihooks-tick-watch` and `agentihooks-inbox-waker` units, with any leftover
`agentihooks-swarm` timer. The scheduler, quota prober, hive heartbeat and herdr client stay, and the workstation keeps
its master and the agents it hosts as a hive. Starting a local swarm brings the units back.

```bash
agentihooks swarm my-swarm tick
# swarm: the swarm my-swarm is served in Anton, so only its controller ticks it
```

`agentihooks swarm <id> tick` runs one pass for a local swarm and exits 1 with that line for a swarm served in Anton.
It never reconciles the units; only the machine wide tick and `swarm start` do.

### Configure the controller

In the `agentihooks-swarm` chart values, `controller.api.swarm` names the served swarm and `controller.api.proof` is
absent. A `proof` block marks a scratch proof swarm that the controller removes when its proof ends; a live swarm
never carries one. The Anton overlay:

```yaml
deployment: distributed
controller:
  api:
    enabled: true
    port: 8780
    swarm: rig-grade-swarm
    signingKey:
      keyId: anton-launch-1
      secretName: agentihooks-swarm-launch-signing
      secretKey: signing-key
  workers:
    apiUrl: http://agentihooks-swarm-controller.agentihooks-swarm.svc:8780
    imageTag: release
    profile: general
    cap: 1
    projects:
      - github.com/the-cloud-clockwork/agentihooks
    brain: swarm
    podPolicyConfigMap: swarm-pod-policy
```

- `controller.api.swarm` becomes `AGENTIHOOKS_CONTROL_SWARM`. The controller serves the control API for that swarm
  only; until the swarm exists in the shared Redis it logs
  `controller: serves the control API for <swarm>, which does not exist yet; waits for it`.
- `controller.hostSwarms` becomes `AGENTIHOOKS_CONTROL_HOST_SWARMS`: swarms in the shared Redis that their own host
  ticks. The controller never ticks them and its liveness probe does not require their lease, so a sleeping
  workstation never hands its swarm to the controller. The chart refuses a `controller.api.swarm` on that list.
- `controller.scratchSwarms` becomes `AGENTIHOOKS_CONTROL_SCRATCH_SWARMS`: a list of `{swarm, agent, task, repo}`
  proof swarms the controller serves beside the live swarm. Each pass it creates a missing one as a scratch swarm with
  one engineer task, and its ledger through the proof ledger route, which names it from the agent and task
  (`proof-<code>-<task>-<n>`, the first free number). `swarm` must be that name: a ledger created under another name
  is removed and the entry logged as not created, while the other entries are still served. A proof swarm that leaves
  the list loses its ledger, is set stopping while agents remain, and is removed once they end. The controller and
  the chart refuse a duplicate, an entry missing a field, a name that is not a proof swarm name, one on
  `controller.hostSwarms` or the served `controller.api.swarm` proof.
- When the ledger is remote (`SWARM_URL`, an `AGENTIHOOKS_DEPLOYMENT` other than `local`, or `LEDGER_URL` with no
  mode set; see [Client mode resolution](#client-mode-resolution)), the controller needs
  `AGENTIHOOKS_CONTROLLER_CREDENTIAL` and refuses to start without it.
- `controller.workers.apiUrl` turns the Kubernetes runtime on. At start the controller logs
  `controller: built the Kubernetes runtime for <swarm>: workers in <namespace> run <image>:<tag> with profile <profile>`.
- The other worker values are in [Configure the worker reading](../pillars/swarm.md#configure-the-worker-reading).

### Which ledger address a tick uses

A tick never resolves or routes to a ledger address: `run_tick` and every other swarm command run the ledger API
route code in process against the shared repository, judged as the swarm's own service (`database_ledger.acting`).
There is nothing left for `api_url` to route for the tick; the setting only carries the controller's own
worker-registration address, written by the controller itself on every pass and read back by
[Configure the controller](#configure-the-controller) and [Which work runs
where](#which-work-runs-where). A command tick outside local client mode still stops at the gate in [Ticks started
by swarm commands](#ticks-started-by-swarm-commands) before it reaches any ledger call.

### Which work runs where

The routed runtime places each launch by lane and profile:

| Work | Where it runs |
|---|---|
| `eng` and `ci` lanes | A worker Pod on the cluster. |
| A task whose profile is `frontend`, on any lane | The workstation. |
| Master seats owned by the controller's own hive | A worker Pod on the cluster. |
| Master seats owned by the workstation, planners (`plan`) and the dispatcher (`dispatch`) | The workstation. |

`controller.workers.cap` is the launch cap per approved account: each account the worker route offers takes at most
that many live workers. With one approved account and `cap: 1`, as on Anton, one worker runs at a time. The swarm's
own caps (`max-eng-agents`, `max-ci-agents`) still apply on top. With hive placement on, worker hive Pods take
`eng`, `ci`, `plan` and `dispatch` as described on the [Hives](../pillars/hives.md#placement) page instead.

### The hive endpoint on Anton and its render checks

Machines join Anton's hives from the UI hive with `agentihooks hive add`, which delivers each member's `hive.env`
over SSH; see [Adding a machine to a hive](../pillars/hives.md#adding-a-machine-to-a-hive). The hive endpoint those
members post `hive refresh` to is the chart's `hive-join` Deployment. The antoncore overlay
`k8s/values-overlays/agentihooks-swarm/values.yaml` enables it:

```yaml
hive:
  join:
    enabled: true
    memberRedisUrl: rediss://swarm-internal.example.com:6379/0
    tlsSecretName: agentihooks-swarm-redis-tls
    serveLedgerCa: false        # hives keep trusting the workstation ledger's CA
    natsSigner:
      secretName: agentihooks-swarm-nats-signer
```

The antoncore manifest `k8s/manifests/agentihooks-swarm/services.yaml` publishes it on the LAN as the
LoadBalancer `agentihooks-swarm-hive-join-lan`, port `join` 8770, on the shared MetalLB address `192.0.2.207`
that [Swarm LAN services](swarm-lan-services.md) describes. That address and port form the `--url` given to
`hive add`.

The antoncore `ci-validate` workflow has no check that renders the `hive-join` Deployment.
[Antoncore pull request 988](https://github.com/the-cloud-clockwork/antoncore/pull/988) removed its
`Render the hive join service` step, and
[antoncore pull request 1002](https://github.com/the-cloud-clockwork/antoncore/pull/1002), merged into antoncore
`dev` on 20 October 2026, deleted the checker that step ran,
`tests/unit/agentihooks_swarm/hive_join_render.py`. The checker had no tests of its own, and no file referenced it
after the step was removed. The pull request recorded this call site evidence at antoncore `dev` `f8727f48c`:

```bash
git grep -n "hive_join_render\|hive-join-render" origin/dev
# no output, exit 1
```

The workflow's `Swarm checkpoint storage` job renders the agentihooks chart and grades the output with the
render checkers that remain:

| Step | Checker in `tests/unit/agentihooks_swarm/` | What it reads |
|---|---|---|
| `Render controller credential isolation` | `checkpoint_render.py` | The checkpoint bucket, prefix and storage credential variables in the rendered pods |
| `Render controller credential isolation` | `controller_github_render.py` | The controller container's GitHub App token path |
| `Render the ledger over TLS` | `ledger_tls_render.py` | The ledger's TLS certificate, key and CA from `agentihooks-swarm-ledger-tls` and its `https` address |

Each step also renders a planted fault and fails when its checker accepts it. The `hive-join` template is graded
inside agentihooks: the helm kind smoke `deploy/helm/agentihooks-swarm/ci/kind-smoke.sh` renders it from
`ci/kind-hive-join.yaml` through `tests/chart_hive_join.py`, and fails when that check accepts a planted fault such
as a hive join serving its ledger CA while `serveLedgerCa` is `false`, or a render with no hive join Deployment.

Read the live endpoint without credentials:

```bash
kubectl -n agentihooks-swarm get deploy -l app.kubernetes.io/component=hive-join
kubectl -n agentihooks-swarm get service agentihooks-swarm-hive-join-lan \
  -o jsonpath='{.status.loadBalancer.ingress[*].ip}{" "}{.spec.ports[?(@.name=="join")].port}{"\n"}'
# 192.0.2.207 8770
```

| Symptom | Cause | Fix |
|---|---|---|
| `hive refresh refused: the hive at <url> is unreachable` from an Anton hive | The `hive-join` Deployment is not running, or the LAN Service holds no `192.0.2.207` ingress | Run the two commands above; for the address, follow [Inspect service ownership](swarm-lan-services.md#inspect-service-ownership). Change the endpoint through the antoncore overlay and its GitOps delivery |
| A change to `hive.join` values in antoncore passes `ci-validate` and still breaks refresh | No antoncore check renders the `hive-join` Deployment | Read the rendered Deployment with `helm template` against the overlay before merging, and run the agentihooks helm kind smoke for template changes |
| A search for the hive join render checker in antoncore finds nothing | It was deleted in antoncore pull request 1002 | Use the checkers in the table above; none replaces it |

### Verify

```bash
agentihooks swarm my-swarm status --json      # the Anton engineer appears with its execution and task
kubectl -n agentihooks-swarm logs deploy/agentihooks-swarm-controller | grep 'controller:'
kubectl -n agentihooks-swarm-workers get pods -l swarm.agentihooks.io/controller-owner
```

- Worker Pods carry the `swarm.agentihooks.io/controller-owner` label naming the controller that launched them.
- `swarm status` lists the cluster engineer next to the workstation agents, with its claimed task.
- The controller log carries the served swarm and the Kubernetes runtime line above, and no secret values.
- The hosted ledger shows the engineer's ledger writes on its task.

## Client mode resolution

The `agentihooks ledger` CLI, the swarm tick, the session hook and a worker on a hive read and write a ledger by
running the ledger API route code in process against the shared repository ([Shared ledger database
sessions](shared-ledger-database.md)): Postgres when `AGENTIHOOKS_SWARM_DATABASE_URL` is set, else the SQLite
folder under `LEDGER_DIR`. `ledger_link.remote` still decides whether a process is a remote client, but that now
governs only the parts of a session's work that still travel as HTTP: the live event stream
(`agentihooks ledger watch`, and the page's own stream) and a `/healthz` probe. A remote client sends those to
`SWARM_URL` or `LEDGER_URL` with a derived or issued agent token; a local client sends them to the ledger server on
`LEDGER_HOST:LEDGER_PORT` with the page credential stored beside that ledger. In process, no credential header is
checked: a call is judged by the pinned agent name or by `database_ledger.acting`, never by `remote`.

The first rule that matches decides:

| Order | Environment | Client |
|---|---|---|
| 1 | `AGENTIHOOKS_HOSTED_LEDGER_URL` set, the session holds `AGENTIHOOKS_LEDGER_AGENT_TOKEN` or `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL`, and the address answers `/healthz` | Remote, to the hosted address, in every mode |
| 2 | `SWARM_URL` set, and `AGENTIHOOKS_SWARM` is unset or names this ledger | Remote, in every mode, `local` included |
| 3 | `AGENTIHOOKS_DEPLOYMENT` unset and `LEDGER_URL` set | Remote |
| 4 | `AGENTIHOOKS_DEPLOYMENT` unset and `LEDGER_URL` unset | Local |
| 5 | `AGENTIHOOKS_DEPLOYMENT=local` | Local; `LEDGER_URL` is ignored |
| 6 | `AGENTIHOOKS_DEPLOYMENT` set to any other value | Remote |

A hub address with no mode is remote because it is the safe default: a worker handed the hub's `LEDGER_URL` and
nothing else must not fall back to a local client that reads a local ledger credential. Local mode needs either no
hub address at all or an explicit `AGENTIHOOKS_DEPLOYMENT=local`, and rule 1 still sends a session with a hosted
credential to an answering hosted ledger in local mode.

A remote client sends to `SWARM_URL`, else `LEDGER_URL`, with any trailing slash removed.

### Hosted ledger first

`AGENTIHOOKS_HOSTED_LEDGER_URL` names the hosted ledger every agent tries first. A process checks it once, with a
2 second `/healthz` request. It answers only with a JSON object; a refused, reset or timed out connection, an error
status, or a page such as an access login does not. The answer is
kept for 30 seconds in `hosted-ledger-probe.json` under the agentihooks home, so hooks and commands inside that
window reuse it instead of waiting on a silent ledger. When it does not answer, the process falls through to rule 3
onwards: a workstation agent reaches its workstation ledger server, a hive agent its `SWARM_URL` or `LEDGER_URL`.
A command after the window checks again. A session without a hosted credential never takes rule 2, so it keeps the
workstation server and its page credential.

The launch settings carry the address and the credential:

- On the workstation, set `AGENTIHOOKS_HOSTED_LEDGER_URL` in the env file the tick loads. The tick then reads the
  hosted server secret (`SWARM_SECRET`, else OpenBao) and hands each spawned agent its
  `AGENTIHOOKS_LEDGER_AGENT_TOKEN`; without the secret the tick still runs and its agents keep the workstation server.
- On a hive, set `AGENTIHOOKS_HOSTED_LEDGER_URL` in the hive's `hive.env`. Every launch hands it to the
  worker home beside `AGENTIHOOKS_SWARM_API_URL`.

### The launch route wins over a profile setting

An agent starts with the `env` block of its rendered Claude profile home. A bundle or profile may set
`AGENTIHOOKS_HOSTED_LEDGER_URL` in its `settings.overrides.json` as a default. When the process that renders the
profile has the variable set, that value replaces the profile's in the rendered `settings.json`; a profile value
applies only when the rendering process has none. A hive that runs agents as herdr panes spawns each one through
`init-agent`, whose `select-profile` step renders the profile with the controller's environment, so the address in
the hive's `hive.env`, or in a worker hive pod's environment, reaches its agents.

The rendered home's stamp records the address it was rendered with. A render whose address differs builds a fresh
home instead of reusing the cached one, so a changed address reaches the next agent without a manual rerender. An
unchanged address keeps the cached home. The hive's systemd unit loads `hive.env` as its environment file when the
controller starts, so restart the controller after changing the file.

In this example a worker hive reaches the hosted ledger over its internal name while the bundle names the public
address. In the hive's environment:

```bash
AGENTIHOOKS_HOSTED_LEDGER_URL=https://swarm-internal.example.com:8765
```

In the bundle's `profiles/<profile>/.claude/settings.overrides.json`:

```json
{"env": {"AGENTIHOOKS_HOSTED_LEDGER_URL": "https://swarm.example.com"}}
```

Agents launched on that hive start with the internal address. A workstation tick whose env file leaves the variable
unset launches agents with the public address from the bundle.

To check which address an agent received, read the key from its rendered home (never print the whole file, which
holds other settings):

```bash
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["env"].get("AGENTIHOOKS_HOSTED_LEDGER_URL"))' \
  <agent home>/claude/settings.json
```

Then confirm the launch passed with `agentihooks swarm <slug> status --json`: under `agents`, that agent's
`hive_join` checks `ledger_join` and `ledger_write` are both `true`.

`tests/test_profile_ledger_route.py` covers the rule on a profile rendered in process: a configured address wins
over the bundle's, an empty one keeps the bundle's, a changed address rerenders the cached home, and an agent whose
bundle address refuses connections joins and acknowledges the ledger through its configured route.

Limitation: an agent the Kubernetes runtime launches in its own worker pod builds its home through the worker home
bootstrap. It receives the address as a launch endpoint, not in the rendering process's environment, and the
rendered profile `env` takes precedence over launch endpoints there. Keep a bundle `AGENTIHOOKS_HOSTED_LEDGER_URL`
reachable from such pods, or leave it unset.

### Credentials a remote client sends

A remote client picks the first credential that applies and never reads the ledger's page token:

1. Inside the swarm tick, the tick's signed `X-Ledger-Tick` header.
2. `AGENTIHOOKS_CONTROLLER_CREDENTIAL`, for a process with no pinned agent identity.
3. For a pinned agent, meaning one with both a swarm (`AGENTIHOOKS_SWARM`) and a name (its session name or
   `AGENTIHOOKS_AGENT_NAME`), `X-Ledger-Token` with `X-Ledger-Agent`. The token is
   `AGENTIHOOKS_LEDGER_AGENT_TOKEN`; without it, the token derived from `SWARM_SECRET`; without that, a token
   fetched from the hub with the hive credential `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL`.

This selection no longer gates an ordinary ledger read or write: those run in process against the shared database
with no credential header checked, judged only by the pinned agent name or `database_ledger.acting`. It still
picks the headers for the live event stream (`agentihooks ledger watch`) and for routes that keep their own
credential check in process, such as a proof ledger's controls. Server side checks of these credentials are in
[Hosted ledger auth](hosted-ledger-auth.md#clients).

### Configuration

A worker on a hive that reaches the hub ledger. No mode is needed; the hub address makes it remote:

```bash
LEDGER_URL=https://swarm.example.internal:8765
AGENTIHOOKS_SWARM=rig-grade-swarm
AGENTIHOOKS_AGENT_NAME=engineer@worker-1
AGENTIHOOKS_LEDGER_AGENT_TOKEN=<issued by the launcher, never committed>
```

A workstation that keeps its own local ledger while a hub address is present in its environment, for example from
a shared env file:

```bash
AGENTIHOOKS_DEPLOYMENT=local
LEDGER_URL=https://swarm.example.internal:8765   # ignored under explicit local mode
LEDGER_HOST=127.0.0.1
LEDGER_PORT=8765
```

A workstation that reaches the hosted ledger for its own swarm only.
`SWARM_URL` wins even under explicit local mode:

```bash
AGENTIHOOKS_DEPLOYMENT=local
AGENTIHOOKS_SWARM=rig-grade-swarm
SWARM_URL=https://swarm.example.com
```

To see which way a shell resolves, from an agentihooks checkout:

```bash
python -c 'from scripts.swarm_ledger import ledger_link; print(ledger_link.remote(), ledger_link.base())'
```

### Limitations

- `LEDGER_URL` is not scoped to a ledger. With no mode it makes every ledger the process touches remote, unlike
  `SWARM_URL`, which applies only to the ledger `AGENTIHOOKS_SWARM` names.
- An `AGENTIHOOKS_DEPLOYMENT` that is set but empty counts as a mode other than `local`, so the client is remote.
- A remote client cannot make service writes with the page credential; the operator credential stays on its host.

### Plan read on a remote client

`agentihooks plan read` prints a task's plan chunk, or with `--phase` a phase's whole plan range, from the plan
stored on the ledger. It resolves the client mode with the rules above, without naming a ledger, so a `SWARM_URL`
makes it remote whichever ledger `AGENTIHOOKS_SWARM` names.

| Client | Tasks, phases and artifacts | Stored plan text |
|---|---|---|
| Local | The local ledger folder | The local artifact folder |
| Remote | The hosted ledger API, one collection read each for `tasks`, `phases` and `artifacts` | `GET <ledger address>/artifacts/<slug>/<file id>` on the hosted ledger server |

The ledger address is the one a remote client sends to: an answering `AGENTIHOOKS_HOSTED_LEDGER_URL`, else
`SWARM_URL`, else `LEDGER_URL`. The three collection reads run the ledger API route code in process against the
shared database, so no credential travels for them; only the artifact request is a real network call, and it
carries no ledger credential, only the access headers from `SWARM_ACCESS_CLIENT_ID` and
`SWARM_ACCESS_CLIENT_SECRET` (else the `CF_ACCESS_` pair). An agent on a remote hive therefore reads its plan lines
with no ledger folder and no artifact folder on that machine. A local client reads as before.

On a worker hive whose environment carries the hub address, inside a swarm task session:

```bash
# AGENTIHOOKS_DEPLOYMENT=compose, LEDGER_URL=https://swarm.example.internal:8765,
# AGENTIHOOKS_SWARM and AGENTIHOOKS_SWARM_TASK set by the launch
agentihooks plan read
agentihooks plan read --phase <phase id>
agentihooks plan read --slug rig-grade-swarm --task <task id>
```

A task without plan lines prints `This task has no plan lines; its description is the whole spec.` and the task
description, in both modes.

| Output | Cause |
|---|---|
| `no ledger <slug>` | One of the three collection reads found no ledger by that name |
| `no stored artifact <file id>` | The ledger holds the ledger but the artifact GET answered 404 for the plan file |
| An HTTP error traceback with its status | The artifact GET refused with any other status, such as 503 once the read retries are spent; the error is not rewritten |

`tests/swarm_ledger/test_plan_read.py` covers the remote read in compose mode with no local ledger folder: the
three collection reads, the artifact request with its address and timeout, and both 404 errors.

### Troubleshooting

The credential messages below surface only for the live event stream (`agentihooks ledger watch`, and the page's
own stream); an ordinary ledger read or write runs in process and never raises them.

| Symptom | Cause | Fix |
|---|---|---|
| `a remote ledger client needs SWARM_URL, the address of the hosted ledger server` | A mode other than `local` with neither `SWARM_URL` nor `LEDGER_URL` set | Set the hub address, or set `AGENTIHOOKS_DEPLOYMENT=local` for a workstation ledger |
| `a remote ledger client needs a pinned agent identity; the operator credential stays on its host` | A hub address reached a shell with no swarm or no agent name, so the client is remote and will not use the page token | Run as a swarm agent, or set `AGENTIHOOKS_DEPLOYMENT=local` on a workstation that owns the ledger |
| `a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential ...` | A pinned agent with no agent token, no `SWARM_SECRET` and no hive credential | Launch the agent through the swarm, which exports its token, or run `agentihooks hive add` on the UI hive to deliver the worker's credential |
| `forbidden` (403) on every hub ledger write after `hub: service credential renewal failed` | The renewal thread could not renew `agentihooks-hive:controller:hub-<hostname>` and the key expired | Fix the logged Redis error; the next renewal writes the key again |
| `agentihooks hub run` exits at startup with a Redis `NoPermissionError` (no permissions to access a key) | The hub's Redis user cannot set `agentihooks-hive:controller:*` | Grant GET and SET on it to the hub's Redis user |
| A workstation stops starting its local ledger server after `LEDGER_URL` was added to its env | Rule 3: a hub address with no mode is remote | Set `AGENTIHOOKS_DEPLOYMENT=local`, or remove `LEDGER_URL` |
| Hive agents fail `ledger_join` and `ledger_write` launch checks while the hive controller reaches the hosted ledger | The agent's rendered `settings.json` holds a different `AGENTIHOOKS_HOSTED_LEDGER_URL` than the hive's environment: the hive runs a release before v2.17.330, or the controller process lacks the variable | Set the variable in `hive.env` or the worker pod's environment, run agentihooks v2.17.330 or later (the first release with the rule) on the hive, and restart the controller; the next launch rerenders the home |
| A process with `AGENTIHOOKS_DEPLOYMENT=local` still talks to the hosted ledger | `SWARM_URL` is set for this swarm and wins over the mode | Unset `SWARM_URL` in that process |
| `agentihooks plan read` on a hive prints `no ledger <slug>` | The hive is a remote client and the hosted ledger holds no ledger by that name, or `SWARM_URL` points at another ledger server | Pass `--slug` with the swarm's ledger, or set the hub address the swarm's ledger lives on |
| `agentihooks plan read` on a hive prints `no stored artifact <file id>` | The phase or task names a plan artifact the hosted ledger does not serve | Publish the plan to that ledger again with `publish-plan`; the command exits with this error until then, so work from the task description |

## Regulated site recipe

For a site where agents may reach only an approved model endpoint and nothing is exposed to the internet: api
only hives, Compose on a private network, no public ledger.

### API only hives

Give each hive an api slot and no subscription login. In the hive's `~/.agentihooks/.env`:

```bash
ANTHROPIC_BASE_URL=https://llm-gateway.example.internal
ANTHROPIC_AUTH_TOKEN=<delivered by your secret store, never committed>
```

A base URL on a host other than `api.anthropic.com` with a token makes the slot a `gateway` api slot. With no
`AH_CC_TOKEN_<slug>` subscription on the hive, the api slot is the only route placement can pick. Cap it with
`agentihooks balance set claude-api-max-sessions=N`. A master on such a hive needs `ui=yes` and runs through tier two
of [master placement](../pillars/hives.md#master-placement).

### Compose on a private network

In `.env` next to `compose.yaml`:

```bash
SWARM_DEPLOYMENT=compose
SWARM_BIND=198.51.100.5                               # the private interface, never 0.0.0.0 on a public host
SWARM_PUBLIC_URL=http://swarm.example.internal:8765
SWARM_ALLOWED_HOSTS=swarm.example.internal:8765
SWARM_REDIS_PASSWORD=<from your secret store>
SWARM_TLS_DIR=./deploy/compose/tls                    # cert.pem, key.pem, ca.pem
SWARM_REDIS_SCHEME=rediss
SWARM_CA_FILE=/tls/ca.pem
SWARM_MEMBER_REDIS_URL=rediss://swarm.example.internal:6379/0
```

```bash
docker compose --profile join up -d
# on the UI hive, once each worker is reachable over ssh
agentihooks hive add ssh://worker@worker-1:22 \
  --url https://swarm.example.internal:8770/hive/refresh
# on each worker, once hive add has delivered its hive.env
agentihooks hive install
```

- The ledger answers only the host names in `SWARM_PUBLIC_URL` and `SWARM_ALLOWED_HOSTS`.
- The hive endpoint always serves TLS; with `rediss`, Redis does too. When Redis stops answering, the endpoint
  answers 503 once a Redis call times out after five seconds and keeps admitting requests; see
  [Hive endpoint limits](../pillars/hives.md#hive-endpoint-limits).
- Each added hive gets its own Redis user that writes only its own keys.

### No public ledger

Leave `SWARM_AUTH_MODE` unset so the ledger stays in local mode, and publish no ingress, tunnel or Access
application for it. Operators reach the page on the private network; agents reach it with their derived tokens
and hives with their hive tokens.

## Related pages

- [Docker Compose quick start](../getting-started/compose-quick-start.md)
- [Single machine quick start](../getting-started/single-machine-quick-start.md)
- [Kubernetes quick start](../getting-started/kubernetes-quick-start.md)
- [Add a hive](../getting-started/add-hive-quick-start.md)
- [Hives](../pillars/hives.md)
- [Swarm](../pillars/swarm.md)
- [Hosted ledger auth](hosted-ledger-auth.md)
- [Shared ledger database sessions](shared-ledger-database.md)
