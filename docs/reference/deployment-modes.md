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
- A client with `SWARM_URL` set, or in a mode other than `local`, uses the remote ledger and never starts a local
  ledger server. The hosted service's auth is in [Hosted ledger auth](hosted-ledger-auth.md).
- Whether a hive runs on subscriptions or api keys is a property of its slots, chosen per hive. See
  [Claude account load balancing](../pillars/load-balancing.md).

Joining machines, roles, placement, failover timing and fencing are on the [Hives](../pillars/hives.md) page.

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
- The hive join endpoint always serves TLS; with `rediss`, Redis does too.
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
