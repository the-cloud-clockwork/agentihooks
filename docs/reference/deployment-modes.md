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
| `compose` | `compose.yaml`: services `swarm`, `controller`, `redis`, and `hive` under `--profile join` | The `controller` service | The `swarm` service on `SWARM_BIND:SWARM_PORT` (default `127.0.0.1:8765`); the stack Redis with per hive ACL users | On joined [hives](../pillars/hives.md): engineer, CI and master work, and any task pinned to a hive. Other lanes are refused with "the workstation hive spawns" that lane's work. With no hive placement the controller reconciles without spawning. |
| `distributed` | The `agentihooks-swarm` Helm chart (`deployment: distributed`) | The chart's controller | One ledger replica and the chart Redis, or the hosted ledger through `SWARM_URL` | On hives as in `compose`, including worker hive pods; without hive placement, only through the Kubernetes runtime. |

- In any mode other than `local`, `AGENTIHOOKS_HIVE_REDIS_URL` wins over the swarm Redis URL, and the workstation
  host capacity checks are off.
- A client with `SWARM_URL` set, with `LEDGER_URL` set and no mode, or in a mode other than `local`, uses the
  remote ledger and never starts a local ledger server. See [Client mode resolution](#client-mode-resolution). The
  hosted service's auth is in [Hosted ledger auth](hosted-ledger-auth.md).
- Whether a hive runs on subscriptions or api keys is a property of its slots, chosen per hive. See
  [Claude account load balancing](../pillars/load-balancing.md).

Joining machines, roles, placement, failover timing and fencing are on the [Hives](../pillars/hives.md) page.

## Client mode resolution

Every ledger client, whether the `agentihooks ledger` CLI, the swarm tick, the session hook or a worker on a hive,
asks `ledger_link.remote` whether it is a remote client. A remote client sends every read and write to the hub
ledger over HTTP and never reads the local page credential. A local client talks to the ledger server on
`LEDGER_HOST:LEDGER_PORT` with the page credential stored beside that ledger.

The first rule that matches decides:

| Order | Environment | Client |
|---|---|---|
| 1 | A ledger route is active for this ledger: a ledger cutover, or an `agentihooks ledger` command run by an agent of that swarm with no `SWARM_URL`, routed to the swarm's stored API address | Remote, to the routed address |
| 2 | `SWARM_URL` set, and `AGENTIHOOKS_SWARM` is unset or names this ledger | Remote, in every mode, `local` included |
| 3 | `AGENTIHOOKS_DEPLOYMENT` unset and `LEDGER_URL` set | Remote |
| 4 | `AGENTIHOOKS_DEPLOYMENT` unset and `LEDGER_URL` unset | Local |
| 5 | `AGENTIHOOKS_DEPLOYMENT=local` | Local; `LEDGER_URL` is ignored |
| 6 | `AGENTIHOOKS_DEPLOYMENT` set to any other value | Remote |

A hub address with no mode is remote because it is the safe default: a worker handed the hub's `LEDGER_URL` and
nothing else must not fall back to a local client that reads a local ledger credential. Local mode needs either no
hub address at all or an explicit `AGENTIHOOKS_DEPLOYMENT=local`.

A remote client sends to `SWARM_URL`, else `LEDGER_URL`, with any trailing slash removed.

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
| `a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential ...` | A pinned agent with no agent token, no `SWARM_SECRET` and no hive credential | Launch the agent through the swarm, which exports its token, or run `agentihooks hive join` on the worker |
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
`agentihooks balance set claude-api-max-sessions=N`. A master on such a hive runs through tier two or three of
[master placement](../pillars/hives.md#master-placement).

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
agentihooks hive invite worker-1              # on the swarm host
agentihooks hive join https://swarm.example.internal:8770 <code>   # on each worker
agentihooks hive install
```

- The ledger answers only the host names in `SWARM_PUBLIC_URL` and `SWARM_ALLOWED_HOSTS`.
- The hive join endpoint always serves TLS; with `rediss`, Redis does too. When Redis stops answering, the endpoint
  answers 503 once a Redis call times out after five seconds and keeps admitting requests; see
  [Join service limits](../pillars/hives.md#join-service-limits).
- Each joined hive gets its own Redis user that writes only its own keys.

### No public ledger

Leave `SWARM_AUTH_MODE` unset so the ledger stays in local mode, and publish no ingress, tunnel or Access
application for it. Operators reach the page on the private network; agents reach it with their derived tokens
and hives with their hive tokens.

## Related pages

- [Docker Compose quick start](../getting-started/compose-quick-start.md)
- [Hives](../pillars/hives.md)
- [Swarm](../pillars/swarm.md)
- [Hosted ledger auth](hosted-ledger-auth.md)
