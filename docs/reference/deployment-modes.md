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

- In any mode other than `local`, `AGENTIHOOKS_HIVE_REDIS_URL` wins over the swarm Redis URL, and the workstation
  host capacity checks are off.
- A client with `SWARM_URL` set, with `LEDGER_URL` set and no mode, or in a mode other than `local`, uses the
  remote ledger and never starts a local ledger server. See [Client mode resolution](#client-mode-resolution). The
  hosted service's auth is in [Hosted ledger auth](hosted-ledger-auth.md).
- Whether a hive runs on subscriptions or api keys is a property of its slots, chosen per hive. See
  [Claude account load balancing](../pillars/load-balancing.md).

Adding machines, roles, placement, failover timing and fencing are on the [Hives](../pillars/hives.md) page.

## Serving a live swarm from a distributed controller

A workstation swarm can run its engineer and CI work on a Kubernetes cluster while its master, planners and
dispatcher stay on the workstation. The swarm first moves to the hosted ledger, which hands its controller lease to
the cluster's controller; the chart's controller then serves that swarm and launches only its `eng` and `ci` lanes
as worker Pods. The Anton deployment runs `rig-grade-swarm` this way; the values and names below are Anton's.

### Move the swarm to the hosted ledger

Run these from the workstation that holds the swarm's controller lease, in a plain terminal: activation refuses a
session whose environment names both a swarm and an agent.

```bash
agentihooks swarm my-swarm set api-url-pending=https://swarm.example.internal
agentihooks swarm my-swarm ledger-ready        # JSON; "ready": true once every successor is staged
agentihooks swarm my-swarm pause
agentihooks swarm my-swarm set api-url=https://swarm.example.internal
```

- `api-url-pending` stages the destination and records when it was staged. The active ledger address stays as it
  was, so the swarm keeps writing its current ledger.
- Each live agent verifies its staged credential against the hosted metadata API and writes a readiness receipt.
  `ledger-ready` prints the readiness record built from those receipts: `ready`, `missing` (live agents without a
  current receipt for the staged address), `missing_masters` (configured master seats with no live master),
  `pending_url`, `active_url` and `staged_at`.
- `set api-url` to the staged address activates it. The controller lease first moves to the hosted ledger; then one
  local Redis transaction clears the workstation's controller ownership, writes `api_url`, clears the staged address
  and drops the readiness receipts. When the transaction is refused with `the ledger activation changed before
  commit`, the moved lease is released again. From then on every swarm command, tick and wait for this swarm routes
  to the hosted ledger. Other swarms on the workstation stay local.

Activation refuses, and changes nothing, with:

| Refusal | Cause |
|---|---|
| `the ledger endpoint must be activated separately` | `api-url` was set together with another setting. |
| `the ledger destination must be staged and ready` | The address differs from `api-url-pending`, or nothing is staged. |
| `ledger activation requires controller authority` | The command ran inside a swarm agent session. |
| `the ledger successors are not ready` | `ledger-ready` would still report `"ready": false`. |
| `the swarm must be paused before ledger activation` | The swarm is running or stopping. |
| `the swarm tick is still running` | A tick pass holds the tick lock; retry after it ends. |
| `the source controller ownership changed` | Another hive took the controller lease. |
| `the ledger activation cohort changed` or `the ledger activation changed before commit` | Agents, executions, seats or the lease changed during activation; run it again. |

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
- When the ledger is remote (`SWARM_URL`, an `AGENTIHOOKS_DEPLOYMENT` other than `local`, or `LEDGER_URL` with no
  mode set; see [Client mode resolution](#client-mode-resolution)), the controller needs
  `AGENTIHOOKS_CONTROLLER_CREDENTIAL` and refuses to start without it.
- `controller.workers.apiUrl` turns the Kubernetes runtime on. At start the controller logs
  `controller: built the Kubernetes runtime for <swarm>: workers in <namespace> run <image>:<tag> with profile <profile>`.
- The other worker values are in [Configure the worker reading](../pillars/swarm.md#configure-the-worker-reading).

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

Every ledger client, whether the `agentihooks ledger` CLI, the swarm tick, the session hook or a worker on a hive,
asks `ledger_link.remote` whether it is a remote client. A remote client sends every read and write to the hub
ledger over HTTP and never reads the local page credential. A local client talks to the ledger server on
`LEDGER_HOST:LEDGER_PORT` with the page credential stored beside that ledger.

The first rule that matches decides:

| Order | Environment | Client |
|---|---|---|
| 1 | A ledger route is active for this ledger: a ledger cutover, or an `agentihooks ledger` command run by an agent of that swarm with no `SWARM_URL`, routed to the swarm's stored API address | Remote, to the routed address |
| 2 | `AGENTIHOOKS_HOSTED_LEDGER_URL` set, the session holds `AGENTIHOOKS_LEDGER_AGENT_TOKEN` or `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL`, and the address answers `/healthz` | Remote, to the hosted address, in every mode |
| 3 | `SWARM_URL` set, and `AGENTIHOOKS_SWARM` is unset or names this ledger | Remote, in every mode, `local` included |
| 4 | `AGENTIHOOKS_DEPLOYMENT` unset and `LEDGER_URL` set | Remote |
| 5 | `AGENTIHOOKS_DEPLOYMENT` unset and `LEDGER_URL` unset | Local |
| 6 | `AGENTIHOOKS_DEPLOYMENT=local` | Local; `LEDGER_URL` is ignored |
| 7 | `AGENTIHOOKS_DEPLOYMENT` set to any other value | Remote |

A hub address with no mode is remote because it is the safe default: a worker handed the hub's `LEDGER_URL` and
nothing else must not fall back to a local client that reads a local ledger credential. Local mode needs either no
hub address at all or an explicit `AGENTIHOOKS_DEPLOYMENT=local`, and rule 2 still sends a session with a hosted
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
- On a hive, set `AGENTIHOOKS_HOSTED_LEDGER_URL` in the controller's `controller.env`. Every launch hands it to the
  worker home beside `AGENTIHOOKS_SWARM_API_URL`.

### Credentials a remote client sends

A remote client picks the first credential that applies and never reads the ledger's page token:

1. Inside the swarm tick, the tick's signed `X-Ledger-Tick` header.
2. `AGENTIHOOKS_CONTROLLER_CREDENTIAL`, for a process with no pinned agent identity.
3. For a pinned agent, meaning one with both a swarm (`AGENTIHOOKS_SWARM`) and a name (its session name or
   `AGENTIHOOKS_AGENT_NAME`), `X-Ledger-Token` with `X-Ledger-Agent`. The token is
   `AGENTIHOOKS_LEDGER_AGENT_TOKEN`; without it, the token derived from `SWARM_SECRET`; without that, a token
   fetched from the hub with the hive credential `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL`.

Service writes, which a local client signs with the page credential, are refused on a remote client unless one of
the first two credentials applies. Server side checks of these credentials are in
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
SWARM_URL=https://swarm.homeofanton.com
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

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `a remote ledger client needs SWARM_URL, the address of the hosted ledger server` | A mode other than `local` with neither `SWARM_URL` nor `LEDGER_URL` set | Set the hub address, or set `AGENTIHOOKS_DEPLOYMENT=local` for a workstation ledger |
| `a remote ledger client needs a pinned agent identity; the operator credential stays on its host` | A hub address reached a shell with no swarm or no agent name, so the client is remote and will not use the page token | Run as a swarm agent, or set `AGENTIHOOKS_DEPLOYMENT=local` on a workstation that owns the ledger |
| `a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential ...` | A pinned agent with no agent token, no `SWARM_SECRET` and no hive credential | Launch the agent through the swarm, which exports its token, or run `agentihooks hive add` on the UI hive to deliver the worker's credential |
| `a remote ledger client cannot make service writes; the operator credential stays on its host` | A service write from a remote client outside the tick and without a controller credential | Run it on the hub host, or through the tick |
| A workstation stops starting its local ledger server after `LEDGER_URL` was added to its env | Rule 3: a hub address with no mode is remote | Set `AGENTIHOOKS_DEPLOYMENT=local`, or remove `LEDGER_URL` |
| A process with `AGENTIHOOKS_DEPLOYMENT=local` still talks to the hosted ledger | `SWARM_URL` is set for this swarm and wins over the mode | Unset `SWARM_URL` in that process |

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
AGENTIHOOKS_DEPLOYMENT=compose
SWARM_BIND=10.20.0.5                                  # the private interface, never 0.0.0.0 on a public host
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
agentihooks hive add ssh://worker@worker-1:22 --label worker-1 \
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
- [Hives](../pillars/hives.md)
- [Swarm](../pillars/swarm.md)
- [Hosted ledger auth](hosted-ledger-auth.md)
