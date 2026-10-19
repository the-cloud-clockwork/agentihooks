---
title: Docker Compose Quick Start
nav_order: 1
parent: Getting Started
permalink: /docs/getting-started/compose-quick-start/
---

# Docker Compose Quick Start

Start the packaged swarm service bundle for **Claude Code and Codex**. It provides Redis, the ledger web service and a controller. Independent agent sessions can use [native installation](installation.md) without these services.

## Prerequisites

Install Python 3.11 or later, Docker with a running daemon, and the Docker Compose plugin. Install AgentiHooks:

```bash
uv tool install agentihooks
```

Agent hosts separately need Claude Code or Codex, authenticated accounts or API credentials, and their configuration bundle. The service containers do not inherit a host's CLI login.

## Start the services

```bash
agentihooks swarm init
```

Initialization copies the packaged Compose files into `$AGENTIHOOKS_HOME/swarm-stack` (default `~/.agentihooks/swarm-stack`). On first use it writes a random Redis password, selects free ports and sets the image tag to `release`. It waits for healthy services and a responsive ledger page, then prints the ledger URL and client setup command. Rerunning preserves the password, ports and data.

## Connect a client

Open the printed ledger URL. In the shell that will use the stack, run the printed client setup command; with the default state directory:

```bash
set -a
. "$HOME/.agentihooks/swarm-stack/client.env"
set +a
```

Use the printed location if `AGENTIHOOKS_HOME` overrides the default. Source this generated file locally; do not paste its credentials into chat or logs.

## Choose standalone or swarm work

A standalone Claude Code or Codex session uses profiles and hooks without a swarm. A swarm adds a shared task ledger, a master and coordinated workers. Initialization creates no swarm and launches no agents. Connect an admitted execution host before starting swarm work; Compose services alone do not provision workers. Use the [swarm guide](../pillars/swarm.md) for creation, host setup and deployment controls.

## Diagnose or stop the stack

```bash
cd "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack"
docker compose ps
docker compose logs --tail 50 swarm controller redis
docker compose down
```

Initialization reports missing Docker, daemon or Compose dependencies and unhealthy services with a suggested fix. `down` keeps named volumes; avoid `--volumes` when retaining ledger and Redis data. Keep service ports on loopback for local use; remote host admission and TLS require separate setup.

## Share one database across hosts

Set `AGENTIHOOKS_SWARM_DATABASE_URL` to the shared Postgres connection URL and `AGENTIHOOKS_SWARM_REDIS_URL` to the shared Redis connection URL before initialization. Both URLs are retained in the private stack settings and generated client settings. Every ledger server and swarm engine must use the same database and Redis. Without the database setting, the ledger uses SQLite in `LEDGER_DIR`.

Supply credentials through environment settings or your secret manager. Hosted Kubernetes services use ExternalSecret references. Do not print connection URLs or paste generated client settings into chat. Keep TLS certificate verification enabled for remote connections.

Provision the database before activation. Existing SQLite ledgers require one initial conversion with full history and per ledger task, event and artifact counts verified before changing writers. Keep SQLite read only afterward; run no recurring copies or synchronization. Confirm the cutover with one agent write visible within seconds through both the local page and the hosted Anton page, and verify both show the same live tasks and agents.
