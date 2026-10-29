---
title: Single Machine Quick Start
nav_order: 5
parent: Getting Started
permalink: /docs/getting-started/single-machine-quick-start/
---

# Single Machine Quick Start

Run a whole swarm on one machine: Redis, a NATS server with JetStream, the ledger and the controller in Docker Compose, with this machine as the hive that runs the agents. The [Docker Compose quick start](compose-quick-start.md) describes every option of the stack; this page is the shortest path from an empty machine to a started swarm.

## Prerequisites

- Python 3.12 or later and [uv](https://github.com/astral-sh/uv).
- Docker with a running daemon and the Compose plugin. Check both:

  ```bash
  docker info --format '{{.ServerVersion}}'
  docker compose version
  ```

- Claude Code or Codex installed and signed in on this machine, and a clone of the repository the swarm will work.
- An accepted plan file, such as `~/plans/my-plan.md`.

Install AgentiHooks and its native hooks:

```bash
uv tool install agentihooks
agentihooks init
```

## Start the stack and the swarm

```bash
agentihooks swarm init my-swarm --plan ~/plans/my-plan.md --repo ~/dev/my-repo \
  --operator-asked "start a swarm for my plan"
```

The command checks Docker, writes the stack files under `~/.agentihooks/swarm-stack`, starts `redis`, `nats`, `swarm` and `controller`, waits until all four are healthy and creates the swarm `my-swarm` and its ledger, paused. NATS runs with JetStream on the `nats-data` volume and is reachable only inside the stack. Running the command again is safe: it keeps the password, the ports and the data.

Point this shell at the stack. `client.env` holds the Redis and ledger addresses with their credential, so source it and never paste it:

```bash
set -a
. "$HOME/.agentihooks/swarm-stack/client.env"
set +a
```

## Make this machine a hive

```bash
agentihooks hive install
agentihooks hive list
agentihooks hive set <id> ui=yes max-agents=4
```

`hive install` writes and enables the `agentihooks-hive` user service, which runs `agentihooks hive run` and keeps this machine reporting to the swarm. `hive list` prints this machine's hive id first on its line; pass it to `hive set` so local work and the master have a hive to land on.

## Verify

```bash
docker compose --project-name agentihooks-swarm \
  --project-directory "$HOME/.agentihooks/swarm-stack" ps
curl -fsS "http://127.0.0.1:$(sed -n 's/^SWARM_PORT=//p' "$HOME/.agentihooks/swarm-stack/.env")/healthz"
agentihooks hive list
```

All four services show `healthy`, the ledger answers its health route and `hive list` shows this machine `live`. Open the ledger page that `swarm init` printed, then start the swarm when you want agents to run:

```bash
agentihooks swarm my-swarm start
agentihooks swarm my-swarm status
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Swarm stack not ready: the Docker Compose plugin is missing` | Install Docker Compose v2; `docker compose version` must answer |
| `Swarm stack not ready: redis is <state>` or a port in use | Set `SWARM_REDIS_PORT` or `SWARM_PORT` in `~/.agentihooks/swarm-stack/.env` to a free port, keep the password, run `swarm init` again |
| `nats` is not healthy | `docker compose --project-name agentihooks-swarm --project-directory ~/.agentihooks/swarm-stack logs nats`; JetStream needs the `nats-data` volume writable |
| `hive list` shows the machine `stale` or not at all | `systemctl --user status agentihooks-hive`; run `agentihooks hive install` again after sourcing `client.env` |
| `Swarm not created:` | The name must be one or two lowercase words ending in `swarm`; the stack stays up and the next run reuses it |

The full message table is in the [Docker Compose quick start](compose-quick-start.md#diagnose-or-stop-the-stack). To add a second machine, follow [Add a hive](add-hive-quick-start.md).
